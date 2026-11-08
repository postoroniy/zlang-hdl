"""Streaming byte identities shared by artifact and external-tool owners."""
from __future__ import annotations

import hashlib
from pathlib import Path


def file_digest(path: Path) -> str:
    """Return SHA-256 without materializing a large artifact in memory.

    Path admission, symlink policy and publication remain the caller's concern.
    """
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
