"""Read-only universal2 architecture and deployment-target checks for macOS CI."""

import argparse
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from zipfile import ZipFile


MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",  # 32-bit Mach-O
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",  # 64-bit Mach-O
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",  # Fat Mach-O
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",  # 64-bit fat Mach-O
}
WHEEL_PYTHONS = {"cp311", "cp312", "cp313", "cp314"}


def parse_version(version):
    """Normalize macOS versions without treating 10.15 as a decimal number."""
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
        raise ValueError(f"Invalid macOS version: {version}")
    numbers = tuple(int(part) for part in version.split("."))
    return numbers + (0,) * (3 - len(numbers))


def format_version(version):
    return ".".join(str(part) for part in version)


def parse_macos_minimum(output):
    """Read the minimum, not the SDK, from modern or legacy otool commands."""
    minimums = []
    for block in re.split(r"(?m)^\s*Load command \d+\s*$", output):
        command = re.search(r"(?m)^\s*cmd\s+(LC_\w+)\s*$", block)
        if command is None:
            continue
        if command[1] == "LC_BUILD_VERSION":
            platform = re.search(r"(?m)^\s*platform\s+(\S+)\s*$", block)
            if platform is None or platform[1].lower() not in {"1", "macos"}:
                continue
            field = "minos"
        elif command[1] == "LC_VERSION_MIN_MACOSX":
            field = "version"
        else:
            continue
        minimum = re.search(rf"(?m)^\s*{field}\s+(\S+)\s*$", block)
        if minimum is None:
            raise ValueError(f"Missing {field} in {command[1]}")
        minimums.append(parse_version(minimum[1]))
    if len(minimums) != 1:
        raise ValueError("Expected exactly one macOS minimum-version command")
    return minimums[0]


def run_xcrun(*arguments, show_output=True):
    """Log commands and build-version output without dumping all load commands."""
    command = ["xcrun", *(str(argument) for argument in arguments)]
    print("+", shlex.join(command), flush=True)
    output = subprocess.check_output(command, text=True)
    if output and show_output:
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
    return output


def is_macho(path):
    """Recognize extensions, dylibs, and extensionless bundled executables."""
    with path.open("rb") as stream:
        return stream.read(4) in MACHO_MAGICS


def inspect_macho_binary(binary, baseline, mode="exact"):
    """Require both slices and check each slice's actual minimum macOS version."""
    binary = binary.resolve()
    if not binary.is_file():
        raise ValueError(f"Native binary not found: {binary}")
    if mode not in {"exact", "compatible"}:
        raise ValueError(f"Unknown deployment-target comparison mode: {mode}")
    requested = parse_version(baseline)
    run_xcrun("lipo", "-verify_arch", "x86_64", "arm64", binary)
    minimums = {}
    for arch in ("x86_64", "arm64"):
        run_xcrun("vtool", "-arch", arch, "-show-build", binary)
        output = run_xcrun(
            "otool", "-arch", arch, "-l", binary, show_output=False)
        minimum = parse_macos_minimum(output)
        expected = max(requested, (11, 0, 0)) if arch == "arm64" else requested
        matches = minimum == expected if mode == "exact" else minimum <= expected
        if not matches:
            comparison = "==" if mode == "exact" else "<="
            raise ValueError(
                f"{binary} ({arch}) has minimum {format_version(minimum)}; "
                f"expected {comparison} {format_version(expected)}")
        minimums[arch] = format_version(minimum)
        print(f"{binary} ({arch}): minimum macOS {format_version(minimum)}",
              flush=True)
    return {
        "binary": str(binary), "minimums": minimums,
        "baseline": baseline, "mode": mode,
    }


def inspect_thorvg_binaries(dist, baseline):
    """Check the raw dylib and flat framework before a consuming build uses them."""
    checks = []
    for relative in ("lib/libthorvg-1.dylib",
                     "Frameworks/KivyThorVG.framework/KivyThorVG"):
        checks.append(inspect_macho_binary(dist / relative, baseline))
    return checks


def selected_wheels(wheelhouse):
    """Require the four CPython universal2 artifacts selected by this workflow."""
    wheels = {}
    for wheel in sorted(wheelhouse.glob("*.whl")):
        parts = wheel.stem.rsplit("-", 3)
        if (len(parts) != 4 or parts[1] not in WHEEL_PYTHONS
                or any(not tag.startswith("macosx_")
                       or not tag.endswith("_universal2")
                       for tag in parts[3].split("."))):
            raise ValueError(f"Unexpected wheel in the universal2 build: {wheel}")
        python = parts[1]
        if python in wheels:
            raise ValueError(f"Multiple universal2 wheels for {python}")
        wheels[python] = wheel
    missing = WHEEL_PYTHONS - wheels.keys()
    if missing:
        raise ValueError(f"Missing universal2 wheels: {', '.join(sorted(missing))}")
    return wheels


def inspect_repaired_wheels(wheelhouse, baseline):
    """Inspect the embedded ThorVG copy in every repaired wheel."""
    checks = []
    for python, wheel in sorted(selected_wheels(wheelhouse).items()):
        print("Repaired wheel:", wheel, flush=True)
        with tempfile.TemporaryDirectory(prefix=f"kivy-{python}-") as directory:
            binary = Path(directory) / "KivyThorVG"
            with ZipFile(wheel) as archive:
                member = "kivy/.dylibs/KivyThorVG"
                if member not in archive.namelist():
                    raise ValueError(f"Missing {member} in {wheel}")
                with archive.open(member) as source, binary.open("wb") as target:
                    shutil.copyfileobj(source, target)
            check = inspect_macho_binary(binary, baseline)
            check["wheel"] = wheel.name
            checks.append(check)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    binary = commands.add_parser("binary", help="Check one universal2 binary")
    binary.add_argument("binary", type=Path)
    binary.add_argument("baseline")
    binary.add_argument("mode", choices=("exact", "compatible"))

    thorvg = commands.add_parser("thorvg", help="Check the raw dylib and framework")
    thorvg.add_argument("dist", type=Path)
    thorvg.add_argument("baseline")

    wheels = commands.add_parser("wheels", help="Check all four repaired wheels")
    wheels.add_argument("wheelhouse", type=Path)
    wheels.add_argument("baseline")

    args = parser.parse_args()
    try:
        if args.command == "binary":
            inspect_macho_binary(args.binary, args.baseline, args.mode)
        elif args.command == "thorvg":
            inspect_thorvg_binaries(args.dist, args.baseline)
        elif args.command == "wheels":
            inspect_repaired_wheels(args.wheelhouse, args.baseline)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Native binary check failed: {error}\n")


if __name__ == "__main__":
    main()
