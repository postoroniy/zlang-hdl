#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Extract one exact release section from the project changelog."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


HEADING = re.compile(r"^##\s+([^\s]+)(?:\s+.*)?$")
RELEASE_TAG = re.compile(r"^v?([0-9]+\.[0-9]+\.[0-9]+(?:a[0-9]+)?)$")


def release_notes(changelog: str, tag: str) -> str:
    """Return the body of the unique changelog section selected by *tag*."""

    match = RELEASE_TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"invalid release tag: {tag!r}")
    version = match.group(1)

    lines = changelog.splitlines()
    matches = [
        index
        for index, line in enumerate(lines)
        if (match := HEADING.match(line)) and match.group(1) == version
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one changelog section for {version}, found {len(matches)}"
        )

    start = matches[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise ValueError(f"changelog section for {version} is empty")
    return f"{body}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    notes = release_notes(args.changelog.read_text(encoding="utf-8"), args.tag)
    args.output.write_text(notes, encoding="utf-8")


if __name__ == "__main__":
    main()
