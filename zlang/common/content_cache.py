"""Small atomic helpers for content-addressed compiler caches.

The cache formats remain owned by their compiler layers.  This module only
provides deterministic JSON loading and same-directory atomic publication so a
concurrent reader never observes a partially written entry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from .serialization import stable_json


def load_json_object(path: Path) -> tuple[Mapping[str, object] | None, str | None]:
    """Return one JSON object, or a diagnostic for an unreadable entry."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"cannot read cache entry: {error}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        return None, f"invalid JSON at line {error.lineno} column {error.colno}"
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        return None, "cache entry must be a JSON object with string keys"
    return value, None


def publish_json_atomically(path: Path, value: Mapping[str, object]) -> None:
    """Durably replace one JSON cache entry without exposing partial text."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(stable_json(value) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, destination)
        temporary_name = None
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(destination.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


__all__ = ["load_json_object", "publish_json_atomically"]
