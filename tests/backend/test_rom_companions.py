from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from zlang.backend.companions import (
    CompanionArtifactError,
    collect_rom_companions,
    publish_companion_bundle,
    validate_published_companions,
)
from zlang.backend.manifest import BackendArtifact, COMPANION_MANIFEST_VERSION
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source


ROM_SOURCE = """
module RomTop {
    clock clk
    reset rst
    in address : u2
    out y : u8

    rom table : rom<u8,4> {
        read_latency 1
        init generate(i in 0..4) i
    }
    table.read_address = address
    y = table.read_data
}
"""


def test_exact_width_address_order_and_artifact_round_trip() -> None:
    module = compile_source(ROM_SOURCE, include_clash=False).ir
    companions = collect_rom_companions(module)
    assert len(companions) == 1
    image = companions[0]
    assert image.text == "00000000\n00000001\n00000010\n00000011\n"
    assert image.file_hash == hashlib.sha256(image.text.encode("ascii")).hexdigest()

    artifact = emit_artifact(module)
    assert artifact.manifest_version >= COMPANION_MANIFEST_VERSION
    assert artifact.artifact_hash == hashlib.sha256(artifact.text.encode()).hexdigest()
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.companions == (image.__class__(
        logical_path=image.logical_path,
        file_hash=image.file_hash,
        semantic_id=image.semantic_id,
        object_kind=image.object_kind,
        canonical_type=image.canonical_type,
        word_width=image.word_width,
        depth=image.depth,
        read_latency=image.read_latency,
        initialization_identity=image.initialization_identity,
        dependency_identity=image.dependency_identity,
        evaluator_schema=image.evaluator_schema,
        content_hash=image.content_hash,
        source_origin=image.source_origin,
    ),)


def test_publication_is_complete_and_collision_safe(tmp_path: Path) -> None:
    image = collect_rom_companions(
        compile_source(ROM_SOURCE, include_clash=False).ir
    )[0]
    paths = publish_companion_bundle((image,), tmp_path)
    assert paths == (tmp_path / image.logical_path,)
    validate_published_companions((image,), tmp_path)
    # An identical retry is deterministic and harmless.
    original_inode = paths[0].stat().st_ino
    assert publish_companion_bundle((image,), tmp_path) == paths
    assert paths[0].stat().st_ino == original_inode
    # BackendArtifact JSON intentionally omits image text; hash-only metadata
    # remains sufficient for no-follow validation of an already-published file.
    validate_published_companions((replace(image, text=""),), tmp_path)

    paths[0].write_text("0\n", encoding="ascii")
    with pytest.raises(CompanionArtifactError, match="collides"):
        publish_companion_bundle((image,), tmp_path)
    with pytest.raises(CompanionArtifactError, match="manifest hash"):
        validate_published_companions((image,), tmp_path)


def test_publication_rejects_symlink_leaf_without_touching_matching_target(
    tmp_path: Path,
) -> None:
    image = collect_rom_companions(
        compile_source(ROM_SOURCE, include_clash=False).ir
    )[0]
    directory = tmp_path / "published"
    directory.mkdir()
    outside = tmp_path / "outside.mem"
    outside.write_text(image.text, encoding="ascii")
    target = directory / image.logical_path
    target.symlink_to(outside)

    with pytest.raises(CompanionArtifactError, match="symbolic link"):
        publish_companion_bundle((image,), directory)

    assert outside.read_text(encoding="ascii") == image.text
    assert target.is_symlink()


def test_publication_rejects_symlinked_parent_without_writing_outside(
    tmp_path: Path,
) -> None:
    image = collect_rom_companions(
        compile_source(ROM_SOURCE, include_clash=False).ir
    )[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = tmp_path / "published"
    directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CompanionArtifactError, match="symbolic link"):
        publish_companion_bundle((image,), directory)

    assert not (outside / image.logical_path).exists()
