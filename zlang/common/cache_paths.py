"""Process-local policy for user cache locations.

Cache owners retain their schemas, bounds, validation, and garbage collection.
This module owns only the shared XDG-compatible root selection.
"""

from __future__ import annotations

import os
from pathlib import Path


def user_cache_root(*components: str) -> Path:
    """Return one namespaced user cache path for the current installation."""

    selected = os.environ.get("XDG_CACHE_HOME")
    if selected:
        candidate = Path(selected).expanduser()
        if candidate.is_absolute():
            return candidate.joinpath("zlang-hdl", *components)
    return Path.home().joinpath(".cache", "zlang-hdl", *components)


__all__ = ["user_cache_root"]
