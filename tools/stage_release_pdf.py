#!/usr/bin/env python3
"""Stage the exact reviewed Community PDF recorded by release/status.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

try:
    from tools.documentation_manifest import load_manuals
    from tools.release_status import StatusError, _check_documentation
except ModuleNotFoundError:
    from documentation_manifest import load_manuals
    from release_status import StatusError, _check_documentation


_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?")


class ReleasePdfError(ValueError):
    """The reviewed PDF cannot be staged from the tagged source tree."""


def stage_release_pdfs(*, root: Path, status_path: Path, tag: str, output: Path) -> tuple[Path, ...]:
    """Validate the entire source-bound set before staging any reviewed bytes."""
    if _TAG.fullmatch(tag) is None:
        raise ReleasePdfError(f"invalid release tag {tag!r}")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(status, dict) or status.get("schema") != 3:
            raise ReleasePdfError("release status schema must be 3")
        if tag != "v" + str(status.get("release", {}).get("version")):
            raise ReleasePdfError("release tag does not match the reviewed candidate")
        _check_documentation(root, status.get("documentation"), status.get("release", {}).get("version"))
        manuals = load_manuals(root)
        payloads = tuple((manual.pdf.name, (root / manual.pdf).read_bytes()) for manual in manuals)
    except (OSError, ValueError, StatusError) as exc:
        raise ReleasePdfError(f"cannot stage reviewed manual set: {exc}") from exc
    if output.is_symlink() or any((output / name).is_symlink() for name, _ in payloads):
        raise ReleasePdfError("manual staging refuses symlink destinations")
    existing_pdfs = {path.name for path in output.iterdir() if path.suffix.lower() == ".pdf"} if output.exists() else set()
    unexpected = existing_pdfs - {name for name, _ in payloads}
    if unexpected:
        raise ReleasePdfError("staging directory contains an unexpected PDF: " + ", ".join(sorted(unexpected)))
    output.mkdir(parents=True, exist_ok=True)
    destinations = tuple(output / name for name, _ in payloads)
    for destination, (_, payload) in zip(destinations, payloads, strict=True):
        destination.write_bytes(payload)
    return destinations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--status", type=Path, default=Path("release/status.json"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    status = arguments.status
    if not status.is_absolute():
        status = root / status
    output = arguments.output
    if not output.is_absolute():
        output = root / output
    try:
        destinations = stage_release_pdfs(
            root=root, status_path=status, tag=arguments.tag, output=output
        )
    except (ReleasePdfError, OSError) as exc:
        print(f"release PDF staging: error: {exc}", file=sys.stderr)
        return 2
    for destination in destinations:
        print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
