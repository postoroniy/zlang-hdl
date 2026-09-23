#!/usr/bin/env python3
"""Stage the exact reviewed Community PDF recorded by release/status.json."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys


_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?")


class ReleasePdfError(ValueError):
    """The reviewed PDF cannot be staged from the tagged source tree."""


def stage_release_pdf(*, root: Path, status_path: Path, tag: str, output: Path) -> Path:
    """Validate and copy reviewed bytes under a deterministic tag-derived name."""

    if _TAG.fullmatch(tag) is None:
        raise ReleasePdfError(f"invalid release tag {tag!r}")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleasePdfError(f"cannot read release status: {exc}") from exc
    if not isinstance(status, dict):
        raise ReleasePdfError("release status must be a JSON object")
    documentation = status.get("documentation")
    if not isinstance(documentation, dict):
        raise ReleasePdfError("release status has no documentation object")
    source_name = documentation.get("pdf")
    expected_digest = documentation.get("pdf_sha256")
    if not isinstance(source_name, str) or not isinstance(expected_digest, str):
        raise ReleasePdfError("release status has no reviewed PDF identity")
    source_path = PurePosixPath(source_name)
    if source_path.is_absolute() or ".." in source_path.parts:
        raise ReleasePdfError("release status PDF path is unsafe")
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise ReleasePdfError("release status PDF digest is malformed")
    source = root / source_path
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ReleasePdfError(f"cannot read reviewed PDF: {exc}") from exc
    if not payload.startswith(b"%PDF-"):
        raise ReleasePdfError("reviewed PDF has an invalid file signature")
    actual_digest = hashlib.sha256(payload).hexdigest()
    if actual_digest != expected_digest:
        raise ReleasePdfError(
            "reviewed PDF digest does not match release/status.json"
        )
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"zlang-hdl-{tag}-language-reference.pdf"
    destination.write_bytes(payload)
    return destination


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
        destination = stage_release_pdf(
            root=root, status_path=status, tag=arguments.tag, output=output
        )
    except (ReleasePdfError, OSError) as exc:
        print(f"release PDF staging: error: {exc}", file=sys.stderr)
        return 2
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
