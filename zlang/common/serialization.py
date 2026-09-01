"""Canonical serialization used for cache keys and artifact identities."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_json(value: Any, *, indent: int | None = None) -> str:
    """Serialize values with deterministic keys and fallback scalar text."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
        default=str,
    )


def stable_json_bytes(value: Any) -> bytes:
    return stable_json(value).encode("utf-8")


def stable_digest(value: Any, *, length: int | None = None) -> str:
    # Preserve the established text-identity behavior used by candidate
    # hashes; structured values use the shared canonical JSON form.
    payload = value.encode("utf-8") if isinstance(value, str) else stable_json_bytes(value)
    digest = hashlib.sha256(payload).hexdigest()
    return digest if length is None else digest[:length]
