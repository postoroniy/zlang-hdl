"""Setuptools bridge for recursively installed ZLang standard-library sources."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from setuptools import setup


ROOT = Path(__file__).resolve().parent


def _stdlib_data_files() -> list[tuple[str, list[str]]]:
    by_destination: dict[str, list[str]] = defaultdict(list)
    for source in sorted((ROOT / "stdlib").rglob("*.zhl")):
        destination = source.parent.relative_to(ROOT).as_posix()
        by_destination[destination].append(source.relative_to(ROOT).as_posix())
    return [
        (destination, by_destination[destination])
        for destination in sorted(by_destination)
    ]


setup(data_files=_stdlib_data_files())
