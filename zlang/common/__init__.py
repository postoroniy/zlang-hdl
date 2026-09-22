"""Dependency-free representation helpers shared by compiler layers."""

from .serialization import (
    CanonicalSerializationError,
    ObjectReader,
    canonical_identity,
    stable_digest,
    stable_json,
    stable_json_bytes,
)
from .subprocess import subprocess_text

__all__ = [
    "CanonicalSerializationError",
    "ObjectReader",
    "canonical_identity",
    "stable_digest",
    "stable_json",
    "stable_json_bytes",
    "subprocess_text",
]
