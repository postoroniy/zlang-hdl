#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Extract one exact release section from the project changelog."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re


HEADING = re.compile(r"^##\s+([^\s]+)(?:\s+.*)?$")
RELEASE_TAG = re.compile(r"^v?([0-9]+\.[0-9]+\.[0-9]+(?:a[0-9]+)?)$")
DATED_RELEASE_HEADING = re.compile(
    r"^##\s+([^\s]+)\s+—\s+(\d{4}-\d{2}-\d{2})$"
)


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


def validate_release_changelog(
    changelog: str,
    tag: str,
    *,
    expected_date: date,
) -> str:
    """Validate final release placement/date and return the selected notes."""

    notes = release_notes(changelog, tag)
    version_match = RELEASE_TAG.fullmatch(tag)
    if version_match is None:
        raise ValueError(f"invalid release tag: {tag!r}")
    version = version_match.group(1)
    dated = [
        match
        for line in changelog.splitlines()
        if (match := DATED_RELEASE_HEADING.fullmatch(line))
        and match.group(1) == version
    ]
    if len(dated) != 1:
        raise ValueError(
            f"release heading for {version} must use '## {version} — YYYY-MM-DD'"
        )
    try:
        release_date = date.fromisoformat(dated[0].group(2))
    except ValueError as exc:
        raise ValueError(f"release heading for {version} has an invalid date") from exc
    if release_date != expected_date:
        raise ValueError(
            f"release date {release_date.isoformat()} does not match exact candidate "
            f"commit date {expected_date.isoformat()}"
        )

    lines = changelog.splitlines()
    unreleased = [
        index
        for index, line in enumerate(lines)
        if (match := HEADING.match(line)) and match.group(1) == "Unreleased"
    ]
    if len(unreleased) != 1:
        raise ValueError(
            f"expected one Unreleased changelog section, found {len(unreleased)}"
        )
    start = unreleased[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    shipped_content = [
        line.strip()
        for line in lines[start:end]
        if line.strip() and not line.startswith("### ")
    ]
    if shipped_content:
        raise ValueError("Unreleased still contains shipped release content")
    return notes


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
