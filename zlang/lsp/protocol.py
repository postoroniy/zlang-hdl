# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""JSON-RPC framing shared by the LSP worker and its process supervisor."""

from __future__ import annotations

import json
from typing import Any, BinaryIO


JSON_RPC_VERSION = "2.0"


class LspProtocolError(ValueError):
    """A malformed JSON-RPC/LSP message or unsupported protocol value."""


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSON_RPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def response(request_id: Any, result: Any = None) -> dict[str, Any]:
    """Build one successful JSON-RPC response."""

    return {"jsonrpc": JSON_RPC_VERSION, "id": request_id, "result": result}


def notification(method: str, message: str) -> dict[str, Any]:
    """Build one LSP window notification carrying a user-visible message."""

    return {
        "jsonrpc": JSON_RPC_VERSION,
        "method": method,
        "params": {"type": 1, "message": message},
    }


def read_message(stream: BinaryIO) -> object | None:
    """Read one standard LSP message from a binary stream."""

    first = stream.readline()
    if first == b"":
        return None
    headers: dict[str, str] = {}
    line = first
    while line not in {b"\r\n", b"\n"}:
        try:
            name, value = line.decode("ascii").rstrip("\r\n").split(":", 1)
        except (UnicodeDecodeError, ValueError) as error:
            raise LspProtocolError("malformed LSP header") from error
        headers[name.strip().lower()] = value.strip()
        line = stream.readline()
        if line == b"":
            raise LspProtocolError("truncated LSP headers")
    value = headers.get("content-length")
    if value is None:
        raise LspProtocolError("LSP message is missing Content-Length")
    try:
        length = int(value)
    except ValueError as error:
        raise LspProtocolError("LSP Content-Length is not an integer") from error
    if length < 0:
        raise LspProtocolError("LSP Content-Length must not be negative")
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise LspProtocolError("truncated LSP message body")
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LspProtocolError("invalid JSON-RPC message") from error


def write_message(stream: BinaryIO, message: object) -> None:
    """Write one deterministic standard LSP message to a binary stream."""

    payload = json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    stream.write(payload)
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


__all__ = [
    "JSON_RPC_VERSION",
    "LspProtocolError",
    "error_response",
    "notification",
    "read_message",
    "response",
    "write_message",
]
