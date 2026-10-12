"""Deterministic companion artifacts consumed by generated hardware sources.

The semantic ROM owns typed constant contents.  This module is the sole place
that turns those contents into the compiler-owned, backend-neutral binary image
used by direct SystemVerilog.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re

from zlang.backend.publication import (
    SafePublicationError,
    publish_relative_files,
    validate_relative_hashes,
)
from zlang.ir.constants import constant_runtime_value
from zlang.ir.module import Module
from zlang.ir.packing import pack_runtime, packed_width
from zlang.source import SourceOrigin


class CompanionArtifactError(ValueError):
    """A companion image is invalid, collides, or was not published exactly."""


@dataclass(frozen=True)
class CompanionArtifact:
    """One logical file plus the semantic metadata needed to validate it."""

    logical_path: str
    file_hash: str
    semantic_id: str
    object_kind: str
    canonical_type: str
    word_width: int
    depth: int
    read_latency: int
    initialization_identity: str
    dependency_identity: tuple[tuple[str, str], ...]
    evaluator_schema: str
    content_hash: str
    source_origin: SourceOrigin | str | None = None
    text: str = ""

    def __post_init__(self) -> None:
        _validate_logical_path(self.logical_path)
        if self.word_width < 1 or self.depth < 1:
            raise CompanionArtifactError(
                "ROM companion width and depth must both be positive"
            )
        if self.read_latency != 1:
            raise CompanionArtifactError(
                "initialized ROM companion requires read latency exactly one"
            )
        if self.text and _hash_text(self.text) != self.file_hash:
            raise CompanionArtifactError(
                f"ROM companion '{self.logical_path}' file hash does not match its contents"
            )


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _validate_logical_path(logical_path: str) -> None:
    path = PurePosixPath(logical_path)
    if (
        not logical_path
        or path.is_absolute()
        or len(path.parts) != 1
        or path.name != logical_path
        or path.suffix != ".mem"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CompanionArtifactError(
            f"invalid ROM companion logical path '{logical_path}'"
        )


def _logical_name(rom: object) -> str:
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", str(rom.name)).strip("_") or "rom"
    # Dependency and initializer identities retain exact build/proof
    # provenance in the manifest, but the physical filename is part of emitted
    # RTL.  Name the image from the typed ROM and its exact contents so a
    # spelling-only source/dependency edit cannot perturb byte-identical RTL.
    identity = "|".join(
        (
            str(rom.semantic_id),
            str(rom.element_type),
            str(rom.depth),
            str(rom.evaluator_schema),
            str(rom.content_hash),
        )
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"zlang_rom_{stem}_{digest}.mem"


def companion_for_rom(rom: object) -> CompanionArtifact:
    """Build the exact-width, address-zero-first image for one typed ROM."""

    width = packed_width(rom.element_type)
    if len(rom.contents) != rom.depth:
        raise CompanionArtifactError(
            f"ROM '{rom.name}' has {len(rom.contents)} contents for depth {rom.depth}"
        )
    words = tuple(
        pack_runtime(rom.element_type, constant_runtime_value(value))
        for value in rom.contents
    )
    text = "".join(f"{word:0{width}b}\n" for word in words)
    return CompanionArtifact(
        logical_path=_logical_name(rom),
        file_hash=_hash_text(text),
        semantic_id=str(rom.semantic_id),
        object_kind="rom_image",
        canonical_type=str(rom.element_type),
        word_width=width,
        depth=int(rom.depth),
        read_latency=int(rom.read_latency),
        initialization_identity=str(rom.initialization_identity),
        dependency_identity=tuple(rom.dependency_identity),
        evaluator_schema=str(rom.evaluator_schema),
        content_hash=str(rom.content_hash),
        source_origin=rom.source_origin,
        text=text,
    )


def collect_rom_companions(module: Module) -> tuple[CompanionArtifact, ...]:
    """Collect a hierarchy's ROM images, rejecting logical-name collisions."""

    by_path: dict[str, CompanionArtifact] = {}

    def visit(current: Module) -> None:
        for rom in current.roms:
            companion = companion_for_rom(rom)
            previous = by_path.get(companion.logical_path)
            if previous is not None and previous != companion:
                raise CompanionArtifactError(
                    f"ROM companion path collision for '{companion.logical_path}'"
                )
            by_path[companion.logical_path] = companion
        for child in current.children:
            visit(child)

    visit(module)
    return tuple(by_path[path] for path in sorted(by_path))


def publish_companion_bundle(
    companions: tuple[CompanionArtifact, ...], directory: Path
) -> tuple[Path, ...]:
    """Publish a complete bundle atomically without following path symlinks."""

    directory = Path(directory)
    seen: dict[str, CompanionArtifact] = {}
    for companion in companions:
        previous = seen.get(companion.logical_path)
        if previous is not None and previous != companion:
            raise CompanionArtifactError(
                f"ROM companion path collision for '{companion.logical_path}'"
            )
        if not companion.text:
            raise CompanionArtifactError(
                f"ROM companion '{companion.logical_path}' has no publishable contents"
            )
        seen[companion.logical_path] = companion

    files = tuple(
        (Path(name), seen[name].text.encode("ascii")) for name in sorted(seen)
    )
    try:
        return publish_relative_files(directory, files, existing="identical")
    except SafePublicationError as error:
        raise CompanionArtifactError(
            f"ROM companion publication failed: {error}"
        ) from error


def validate_published_companions(
    companions: tuple[CompanionArtifact, ...], directory: Path
) -> None:
    """Fail unless every expected logical image exists with its exact hash."""

    directory = Path(directory)
    files = tuple(
        (Path(companion.logical_path), companion.file_hash)
        for companion in companions
    )
    try:
        validate_relative_hashes(directory, files)
    except (OSError, SafePublicationError) as error:
        raise CompanionArtifactError(
            f"published ROM companion bundle in '{directory}' does not match "
            f"its manifest hash: {error}"
        ) from error


__all__ = [
    "CompanionArtifact",
    "CompanionArtifactError",
    "collect_rom_companions",
    "companion_for_rom",
    "publish_companion_bundle",
    "validate_published_companions",
]
