# SPDX-License-Identifier: Apache-2.0
"""Immutable, fail-closed inventory of the three public user manuals.

This owner reads publication facts only; it never parses ZLang or renders PDFs.
The host renderer and the public release verifier consume the same inventory.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import tomllib


MANIFEST = Path("docs/manuals.toml")
MANUAL_IDS = ("language-reference", "standard-library", "user-guide")
PDF_NAMES = (
    "ZLang-HDL-Language-Reference.pdf",
    "ZLang-HDL-Standard-Library-Guide.pdf",
    "ZLang-HDL-User-Guide.pdf",
)


class DocumentationError(ValueError):
    """A public manual inventory or source boundary is invalid."""


@dataclass(frozen=True)
class Manual:
    identity: str
    title: str
    description: str
    entrypoint: Path
    pdf: Path
    sources: tuple[Path, ...]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path(root: Path, value: object, *, suffix: str) -> Path:
    if not isinstance(value, str):
        raise DocumentationError("manual path must be a string")
    path = PurePosixPath(value)
    if (
        path.is_absolute() or path.as_posix() != value or ".." in path.parts
        or "\\" in value or not value.startswith("docs/")
        or path.suffix != suffix or "internal" in path.parts
        or any(part.startswith(".") for part in path.parts)
    ):
        raise DocumentationError(f"unsafe public manual path: {value!r}")
    relative = Path(value)
    if not (root / relative).resolve().is_relative_to(root.resolve()):
        raise DocumentationError(f"manual path escapes the source tree: {value}")
    return relative


def load_manuals(root: Path, *, require_sources: bool = True) -> tuple[Manual, ...]:
    try:
        data = tomllib.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DocumentationError(f"cannot load {MANIFEST}: {exc}") from exc
    if set(data) != {"schema", "manuals"} or type(data["schema"]) is not int or data["schema"] != 1:
        raise DocumentationError("manual manifest schema must be 1")
    records = data["manuals"]
    if not isinstance(records, list) or len(records) != 3:
        raise DocumentationError("manual manifest must contain exactly three books")
    result: list[Manual] = []
    owners: dict[Path, str] = {}
    entrypoints: set[Path] = set()
    for record, identity, filename in zip(records, MANUAL_IDS, PDF_NAMES, strict=True):
        if not isinstance(record, dict) or set(record) != {
            "id", "title", "description", "entrypoint", "pdf", "sources"
        }:
            raise DocumentationError("manual record has missing or unexpected fields")
        if record["id"] != identity:
            raise DocumentationError("manual identities or order differ from the three-book contract")
        if any(not isinstance(record[k], str) or not record[k].strip() for k in ("title", "description")):
            raise DocumentationError("manual title and description must be nonempty")
        entrypoint = _path(root, record["entrypoint"], suffix=".md")
        pdf = _path(root, record["pdf"], suffix=".pdf")
        if pdf != Path("docs") / filename:
            raise DocumentationError(f"wrong PDF mapping for {identity}")
        values = record["sources"]
        if not isinstance(values, list) or not values:
            raise DocumentationError(f"manual {identity} has no sources")
        sources = tuple(_path(root, value, suffix=".md") for value in values)
        if entrypoint in entrypoints or entrypoint in sources:
            raise DocumentationError("manual entrypoints must be distinct indexes, not duplicated chapters")
        entrypoints.add(entrypoint)
        for source in sources:
            if source in owners:
                raise DocumentationError(f"chapter has two owners: {source}")
            owners[source] = identity
        if require_sources:
            for path in (entrypoint, *sources):
                if not (root / path).is_file():
                    raise DocumentationError(f"manual source is missing: {path}")
        result.append(Manual(identity, record["title"], record["description"], entrypoint, pdf, sources))
    if entrypoints & owners.keys():
        raise DocumentationError("a manual index cannot also be a chapter")
    return tuple(result)


def manual_by_id(root: Path, identity: str) -> Manual:
    for manual in load_manuals(root):
        if manual.identity == identity:
            return manual
    raise DocumentationError(f"unknown public manual: {identity}")
