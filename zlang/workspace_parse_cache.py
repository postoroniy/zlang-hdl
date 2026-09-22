"""Bounded, fail-closed metadata cache for successfully parsed project sources.

This cache never stores source text or AST objects. A cache hit is meaningful
only after the caller has read and hashed the exact physical source bytes. The
record certifies the imports produced by a prior successful parse with the
same grammar and parser runtime; semantic consumers still parse their actual
dependency closure before using an AST.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from threading import Lock
from importlib.resources import files

import lark

from zlang.parser import parser as parser_module


PARSE_INDEX_SCHEMA = "zlang-workspace-parse-index-v1"
_MAX_SHARD_BYTES = 16 * 1024
_MAX_IMPORTS = 256
_MAX_FILES = 2048
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_GRAMMAR_DIGEST = hashlib.sha256(parser_module._GRAMMAR.encode("utf-8")).hexdigest()
_PARSER_CODE_DIGEST = hashlib.sha256(
    files("zlang.parser").joinpath("parser.py").read_bytes()
    + files("zlang.ast").joinpath("nodes.py").read_bytes()
).hexdigest()
_GC_LOCK = Lock()
_GC_WRITES: dict[Path, int] = {}


def _cache_root() -> Path:
    selected = os.environ.get("XDG_CACHE_HOME")
    if selected:
        candidate = Path(selected).expanduser()
        if candidate.is_absolute():
            return candidate / "zlang-hdl" / "workspace" / "parse-v1"
    return Path.home() / ".cache" / "zlang-hdl" / "workspace" / "parse-v1"


def _shard_path(logical_path: str, digest: str) -> Path:
    identity = json.dumps(
        (
            PARSE_INDEX_SCHEMA,
            _GRAMMAR_DIGEST,
            _PARSER_CODE_DIGEST,
            lark.__version__,
            logical_path,
            digest,
        ),
        separators=(",", ":"),
    )
    return _cache_root() / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


def load_parse_index(logical_path: str, digest: str) -> tuple[str, ...] | None:
    """Return exact cached imports, or miss on any uncertainty/corruption."""

    if os.environ.get("ZLANG_WORKSPACE_PARSE_CACHE", "persistent") == "off":
        return None
    path = _shard_path(logical_path, digest)
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_SHARD_BYTES:
            return None
        data = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or set(data) != {
        "schema", "grammar", "parser_code", "lark", "logical_path",
        "source_digest", "imports"
    }:
        return None
    if (
        data["schema"] != PARSE_INDEX_SCHEMA
        or data["grammar"] != _GRAMMAR_DIGEST
        or data["parser_code"] != _PARSER_CODE_DIGEST
        or data["lark"] != lark.__version__
        or data["logical_path"] != logical_path
        or data["source_digest"] != digest
    ):
        return None
    imports = data["imports"]
    if (
        not isinstance(imports, list)
        or len(imports) > _MAX_IMPORTS
        or any(not isinstance(item, str) or not item or len(item) > 512 for item in imports)
    ):
        return None
    return tuple(imports)


def publish_parse_index(
    logical_path: str, digest: str, imports: tuple[str, ...]
) -> None:
    """Atomically publish a successful parse outside the project source tree."""

    if (
        os.environ.get("ZLANG_WORKSPACE_PARSE_CACHE", "persistent") == "off"
        or len(imports) > _MAX_IMPORTS
    ):
        return
    path = _shard_path(logical_path, digest)
    payload = (
        json.dumps(
            {
                "schema": PARSE_INDEX_SCHEMA,
                "grammar": _GRAMMAR_DIGEST,
                "parser_code": _PARSER_CODE_DIGEST,
                "lark": lark.__version__,
                "logical_path": logical_path,
                "source_digest": digest,
                "imports": imports,
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_SHARD_BYTES:
        return
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink():
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
        temporary = None
        _collect_old_shards_periodically(path.parent)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _collect_old_shards_periodically(root: Path) -> None:
    """Collect once initially and every 64 writes, never once per source."""

    with _GC_LOCK:
        writes = _GC_WRITES.get(root, 0) + 1
        _GC_WRITES[root] = writes
        if writes != 1 and writes % 64:
            return
        _collect_old_shards(root)


def _collect_old_shards(root: Path) -> None:
    """Keep a bounded best-effort cache; age does not decide validity."""

    try:
        entries = sorted(
            (item for item in root.glob("*.json") if item.is_file() and not item.is_symlink()),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
        )
        total = sum(item.stat().st_size for item in entries)
        while len(entries) > _MAX_FILES or total > _MAX_TOTAL_BYTES:
            item = entries.pop(0)
            total -= item.stat().st_size
            item.unlink()
    except OSError:
        pass
