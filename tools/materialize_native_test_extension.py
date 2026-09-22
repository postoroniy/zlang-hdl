#!/usr/bin/env python3
"""Extract the audited native extension from one wheel into a test source tree."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import shutil
import zipfile


def materialize(wheel: Path, destination: Path) -> Path:
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise SystemExit(f"native test wheel does not exist: {wheel}")
    destination = destination.resolve()
    with zipfile.ZipFile(wheel) as archive:
        extensions = [
            name
            for name in archive.namelist()
            if PurePosixPath(name).parent == PurePosixPath("_zlang_native_sim")
            and PurePosixPath(name).name.startswith("_zlang_native_sim.")
            and PurePosixPath(name).suffix == ".so"
        ]
        initializer = "_zlang_native_sim/__init__.py"
        if len(extensions) != 1 or archive.namelist().count(initializer) != 1:
            raise SystemExit(
                "expected exactly one importable _zlang_native_sim package in "
                "native test wheel"
            )
        for member in (initializer, extensions[0]):
            target = destination / Path(*PurePosixPath(member).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
        target = destination / Path(*PurePosixPath(extensions[0]).parts)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(materialize(args.wheel, args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
