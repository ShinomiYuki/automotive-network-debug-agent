"""Build a Windows directory bundle containing Python and runtime dependencies."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    version = re.search(r'^VERSION = "([0-9.]+)"$', (source / "scripts" / "tx_mailbox.py").read_text(encoding="utf-8"), re.MULTILINE)[1]
    destination = Path(args.output_dir).resolve()
    if destination.exists():
        raise SystemExit("Build output must be a new directory")
    destination.mkdir(parents=True)
    bundle = destination / source.name
    shutil.copytree(source, bundle, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests", "runtime"))
    work = destination / "build"
    work.mkdir()
    library_bin = Path(sys.prefix) / "Library" / "bin"
    if library_bin.is_dir():
        os.environ["PATH"] = str(library_bin) + os.pathsep + os.environ.get("PATH", "")
    PyInstaller.__main__.run([
        "--noconfirm", "--onedir", "--name", "tx-mailbox",
        "--distpath", str(destination / "dist"),
        "--workpath", str(work), "--specpath", str(work),
        "--paths", str(source / "scripts"),
        "--exclude-module", "PySide6", "--exclude-module", "matplotlib",
        "--exclude-module", "pandas", "--exclude-module", "tkinter",
        "--exclude-module", "numpy", "--exclude-module", "scipy",
        str(source / "scripts" / "tx_mailbox.py"),
    ])
    shutil.copytree(destination / "dist" / "tx-mailbox", bundle / "runtime")
    # Include license files for the distributed dependency closure. The build
    # environment is not copied, and none of its absolute paths are required.
    pending = ["cantools", "lxml", "pyinstaller"]
    distributions = {}
    while pending:
        name = pending.pop()
        distribution = importlib.metadata.distribution(name)
        canonical = distribution.metadata["Name"]
        if canonical in distributions:
            continue
        distributions[canonical] = distribution.version
        from packaging.requirements import Requirement
        for requirement_text in (() if canonical.lower() == "pyinstaller" else distribution.requires or []):
            requirement = Requirement(requirement_text)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
        for relative in distribution.files or []:
            if any(word in relative.name.lower() for word in ("license", "copying", "notice")):
                original = Path(distribution.locate_file(relative))
                if original.is_file():
                    license_path = bundle / "licenses" / canonical / Path(str(relative).replace("\\", "/"))
                    license_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, license_path)
    python_licenses = [Path(sys.prefix) / "LICENSE_PYTHON.txt", Path(sys.base_prefix) / "LICENSE.txt", Path(sys.prefix) / "LICENSE.txt"]
    python_license = next((path for path in python_licenses if path.is_file()), None)
    if python_license is None:
        raise SystemExit("Python redistribution license was not found; do not distribute this incomplete bundle")
    shutil.copy2(python_license, bundle / "licenses" / "Python-LICENSE.txt")
    with (bundle / "runtime" / "BUILD_INFO.json").open("w", encoding="utf-8") as stream:
        json.dump({"version": version, "python": sys.version, "platform": sys.platform, "dependencies": distributions, "runtime": "PyInstaller onedir"}, stream, indent=2)
    archive = shutil.make_archive(str(destination / (source.name + f"-{version}-win-x64")), "zip", destination, source.name)
    print(json.dumps({"bundle": str(bundle), "archive": archive}, indent=2))


if __name__ == "__main__":
    main()
