"""Small same-directory atomic publication primitives.

Callers retain ownership of schema validation, path policy, permissions, and
cache collection.  These helpers only ensure that readers never observe a
partially written file.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def publish_bytes_atomically(
    path: Path,
    content: bytes,
    *,
    mode: int | None = None,
    fsync: bool = True,
) -> None:
    """Replace ``path`` with exact bytes written beside the destination."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            if mode is not None:
                os.chmod(temporary_name, mode)
            temporary.write(content)
            if fsync:
                temporary.flush()
                os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        if fsync:
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


def publish_text_atomically(
    path: Path,
    content: str,
    *,
    mode: int | None = None,
    fsync: bool = True,
) -> None:
    """Replace ``path`` with exact UTF-8 text."""

    publish_bytes_atomically(
        path,
        content.encode("utf-8"),
        mode=mode,
        fsync=fsync,
    )


__all__ = ["publish_bytes_atomically", "publish_text_atomically"]
