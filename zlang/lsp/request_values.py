"""Strict untrusted-value decoding for LSP request adapters."""

from __future__ import annotations

from typing import Any

from .protocol import LspProtocolError


def mapping(value: object, key: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LspProtocolError("LSP parameters must be an object")
    if key is None:
        return value
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise LspProtocolError(f"LSP parameters are missing object '{key}'")
    return nested


def string(value: object, key: str) -> str:
    if not isinstance(value, dict) or not isinstance(value.get(key), str):
        raise LspProtocolError(f"LSP field '{key}' must be a string")
    return value[key]


def optional_version(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise LspProtocolError("document version must be an integer")
    return value


def integer(value: object, key: str) -> int:
    if not isinstance(value, dict):
        raise LspProtocolError("LSP position must be an object")
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise LspProtocolError(f"LSP position field '{key}' must be an integer")
    if candidate < 0:
        raise LspProtocolError(f"LSP position field '{key}' must not be negative")
    return candidate


def best_effort_uri(params: object) -> str:
    if isinstance(params, dict):
        item = params.get("textDocument")
        if isinstance(item, dict) and isinstance(item.get("uri"), str):
            return item["uri"]
    return ""


__all__ = ["best_effort_uri", "integer", "mapping", "optional_version", "string"]
