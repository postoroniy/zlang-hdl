from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from zlang.backend.source_map import GeneratedSourceMap
from zlang.backend.systemverilog import emit_artifact_with_source_map
from zlang.compiler import compile_source
from zlang.build_manifest import PublishedFile
from zlang.generated_navigation_bundle import (
    GeneratedNavigationBundleError,
    GeneratedNavigationBundleManifest,
    SourceSnapshotStatus,
    load_generated_navigation_bundle,
    publish_generated_navigation_bundle,
)


SOURCE = """module BundleTop {
    in a : u8
    in b : u8
    out y : u9
    y = a + b
}
"""


def _publish(
    root: Path,
    *,
    source: str = SOURCE,
    source_unit: str = "bundle_top.zhl",
    empty_map: bool = False,
):
    compilation = compile_source(source, source_unit=source_unit)
    artifact, source_map = emit_artifact_with_source_map(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    if empty_map:
        source_map = replace(source_map, entries=())
    generated = root / "published" / "BundleTop.sv"
    generated.parent.mkdir(parents=True)
    generated.write_text(artifact.text, encoding="utf-8")
    bundle = root / "navigation"
    manifest = publish_generated_navigation_bundle(
        bundle,
        artifact=artifact,
        source_map=source_map,
        generated_path=generated,
        root_source_unit=source_unit,
        root_source_digest=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )
    return bundle, manifest, artifact, source_map


def test_real_artifact_bundle_round_trips_and_binds_complete_lineage(
    tmp_path: Path,
) -> None:
    bundle, manifest, artifact, source_map = _publish(tmp_path)

    loaded = load_generated_navigation_bundle(bundle)

    assert loaded.manifest == manifest
    assert loaded.generated_text == artifact.text
    assert loaded.artifact.artifact_hash == artifact.artifact_hash
    assert loaded.source_map == source_map
    assert loaded.artifact.backend == source_map.backend == "direct_systemverilog"
    assert loaded.artifact.module == source_map.module == "BundleTop"
    assert (
        loaded.artifact.selected_ir_identity
        == source_map.selected_ir_identity
    )
    assert loaded.generated_path == bundle / "generated" / "design.sv"
    assert loaded.source_status("bundle_top.zhl", SOURCE) is SourceSnapshotStatus.MATCH
    assert (
        loaded.source_status("bundle_top.zhl", SOURCE + "\n")
        is SourceSnapshotStatus.STALE
    )
    assert (
        loaded.source_status("unknown.zhl", SOURCE)
        is SourceSnapshotStatus.UNKNOWN_SOURCE
    )


def test_loader_does_not_invoke_compiler_or_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _, _, _ = _publish(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("artifact loading must not compile or emit")

    monkeypatch.setattr("zlang.compiler.compile_source", forbidden)
    monkeypatch.setattr("zlang.backend.systemverilog.emit_artifact", forbidden)

    assert load_generated_navigation_bundle(bundle).generated_text.startswith(
        "`default_nettype none"
    )


def test_empty_source_map_still_validates_root_source_freshness(
    tmp_path: Path,
) -> None:
    bundle, _, _, _ = _publish(tmp_path, empty_map=True)
    loaded = load_generated_navigation_bundle(bundle)

    assert loaded.source_map.entries == ()
    assert loaded.source_status("bundle_top.zhl", SOURCE) is SourceSnapshotStatus.MATCH
    assert (
        loaded.source_status("bundle_top.zhl", SOURCE.replace("a + b", "a - b"))
        is SourceSnapshotStatus.STALE
    )


def test_dependency_snapshots_are_complete_and_deterministic(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE, source_unit="bundle_top.zhl")
    artifact, source_map = emit_artifact_with_source_map(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    dependency_digest = hashlib.sha256(b"dependency").hexdigest()
    artifact = replace(
        artifact,
        library_dependencies=(("std.example", dependency_digest),),
    )
    generated = tmp_path / "BundleTop.sv"
    generated.write_text(artifact.text, encoding="utf-8")
    bundle = tmp_path / "bundle"
    manifest = publish_generated_navigation_bundle(
        bundle,
        artifact=artifact,
        source_map=source_map,
        generated_path=generated,
        root_source_unit="bundle_top.zhl",
        root_source_digest=hashlib.sha256(SOURCE.encode()).hexdigest(),
    )

    assert tuple((item.role, item.source_unit) for item in manifest.sources) == (
        ("root", "bundle_top.zhl"),
        ("dependency", "std.example"),
    )
    loaded = load_generated_navigation_bundle(bundle)
    assert (
        loaded.source_digest_status("std.example", dependency_digest)
        is SourceSnapshotStatus.MATCH
    )


@pytest.mark.parametrize(
    "target", ("generated_artifact", "source_map", "backend_manifest")
)
def test_loader_rejects_tampered_bundle_files(tmp_path: Path, target: str) -> None:
    bundle, manifest, _, _ = _publish(tmp_path)
    record = getattr(manifest, target)
    (bundle / record.logical_path).write_bytes(b"tampered\n")

    with pytest.raises(GeneratedNavigationBundleError, match="expected hash"):
        load_generated_navigation_bundle(bundle)


@pytest.mark.parametrize(
    "target", ("generated_artifact", "source_map", "backend_manifest")
)
def test_loader_rejects_missing_bundle_file(tmp_path: Path, target: str) -> None:
    bundle, manifest, _, _ = _publish(tmp_path)
    (bundle / getattr(manifest, target).logical_path).unlink()

    with pytest.raises(GeneratedNavigationBundleError, match="missing"):
        load_generated_navigation_bundle(bundle)


def test_mismatched_source_map_is_rejected_by_artifact_lineage(tmp_path: Path) -> None:
    first = compile_source(SOURCE, source_unit="bundle_top.zhl")
    first_artifact, _ = emit_artifact_with_source_map(
        first.ir, selected_ir_identity=first.selected_ir_identity
    )
    other_source = SOURCE.replace("BundleTop", "OtherTop")
    second = compile_source(other_source, source_unit="other.zhl")
    _, second_map = emit_artifact_with_source_map(
        second.ir, selected_ir_identity=second.selected_ir_identity
    )
    generated = tmp_path / "BundleTop.sv"
    generated.write_text(first_artifact.text, encoding="utf-8")

    with pytest.raises(GeneratedNavigationBundleError, match="artifact lineage"):
        publish_generated_navigation_bundle(
            tmp_path / "bundle",
            artifact=first_artifact,
            source_map=second_map,
            generated_path=generated,
            root_source_unit="bundle_top.zhl",
            root_source_digest=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )


def test_loader_rejects_hash_valid_sidecar_from_another_artifact(
    tmp_path: Path,
) -> None:
    bundle, manifest, _, _ = _publish(tmp_path / "first")
    other_source = SOURCE.replace("BundleTop", "OtherTop")
    other = compile_source(other_source, source_unit="other.zhl")
    _, other_map = emit_artifact_with_source_map(
        other.ir, selected_ir_identity=other.selected_ir_identity
    )
    map_content = other_map.to_json().encode("utf-8")
    map_path = bundle / manifest.source_map.logical_path
    map_path.write_bytes(map_content)
    changed_manifest = replace(
        manifest,
        source_map=PublishedFile.from_bytes(
            manifest.source_map.logical_path,
            map_content,
            kind="generated_source_map",
        ),
    )
    (bundle / "manifest.json").write_text(changed_manifest.to_json())

    with pytest.raises(GeneratedNavigationBundleError, match="artifact lineage"):
        load_generated_navigation_bundle(bundle)


def test_bundle_is_relocatable_without_original_build_paths(tmp_path: Path) -> None:
    bundle, _, _, _ = _publish(tmp_path / "first")
    relocated = tmp_path / "relocated" / "bundle"
    relocated.parent.mkdir(parents=True)
    shutil.copytree(bundle, relocated)
    shutil.rmtree(tmp_path / "first")

    loaded = load_generated_navigation_bundle(relocated)

    assert loaded.publication_root == relocated
    assert loaded.generated_path.is_relative_to(relocated)
    assert "/home/" not in (relocated / "manifest.json").read_text()


@pytest.mark.parametrize("unsupported_version", (0, 2))
def test_manifest_rejects_path_escape_and_unsupported_schema(
    tmp_path: Path, unsupported_version: int
) -> None:
    bundle, _, _, _ = _publish(tmp_path)
    payload = json.loads((bundle / "manifest.json").read_text())
    payload["generated_artifact"]["logical_path"] = "../escaped.sv"

    with pytest.raises(GeneratedNavigationBundleError, match="must not contain"):
        GeneratedNavigationBundleManifest.from_json(json.dumps(payload))

    payload = json.loads((bundle / "manifest.json").read_text())
    payload["schema_version"] = unsupported_version
    with pytest.raises(GeneratedNavigationBundleError, match="unsupported.*version"):
        GeneratedNavigationBundleManifest.from_json(json.dumps(payload))


def test_loader_rejects_symlinked_bundle_member(tmp_path: Path) -> None:
    bundle, manifest, _, _ = _publish(tmp_path)
    generated = bundle / manifest.generated_artifact.logical_path
    outside = tmp_path / "outside.sv"
    outside.write_bytes(generated.read_bytes())
    generated.unlink()
    generated.symlink_to(outside)

    with pytest.raises(GeneratedNavigationBundleError, match="symbolic link"):
        load_generated_navigation_bundle(bundle)


def test_loader_requires_explicit_bundle_manifest(tmp_path: Path) -> None:
    bundle, _, _, _ = _publish(tmp_path)
    (bundle / "manifest.json").unlink()

    with pytest.raises(GeneratedNavigationBundleError, match="missing manifest"):
        load_generated_navigation_bundle(bundle)


def test_publication_is_deterministic_and_rejects_different_republication(
    tmp_path: Path,
) -> None:
    bundle, manifest, artifact, source_map = _publish(tmp_path)
    first = (bundle / "manifest.json").read_bytes()
    generated = tmp_path / "published" / "BundleTop.sv"

    again = publish_generated_navigation_bundle(
        bundle,
        artifact=artifact,
        source_map=source_map,
        generated_path=generated,
        root_source_unit="bundle_top.zhl",
        root_source_digest=hashlib.sha256(SOURCE.encode()).hexdigest(),
    )
    assert again == manifest
    assert (bundle / "manifest.json").read_bytes() == first

    changed = json.loads((bundle / "manifest.json").read_text())
    changed["bundle_identity"] = "0" * 64
    (bundle / "manifest.json").write_text(json.dumps(changed))
    with pytest.raises(GeneratedNavigationBundleError, match="collides"):
        publish_generated_navigation_bundle(
            bundle,
            artifact=artifact,
            source_map=source_map,
            generated_path=generated,
            root_source_unit="bundle_top.zhl",
            root_source_digest=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )


def test_equivalent_builds_publish_byte_identical_metadata(tmp_path: Path) -> None:
    first, _, _, _ = _publish(tmp_path / "first")
    second, _, _, _ = _publish(tmp_path / "second")

    assert (first / "manifest.json").read_bytes() == (
        second / "manifest.json"
    ).read_bytes()
    assert (first / "manifest/backend-artifact.json").read_bytes() == (
        second / "manifest/backend-artifact.json"
    ).read_bytes()


def test_publisher_rejects_tampered_physical_generated_file(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE, source_unit="bundle_top.zhl")
    artifact, source_map = emit_artifact_with_source_map(
        compilation.ir, selected_ir_identity=compilation.selected_ir_identity
    )
    generated = tmp_path / "BundleTop.sv"
    generated.write_text(artifact.text + "// changed\n", encoding="utf-8")

    with pytest.raises(GeneratedNavigationBundleError, match="artifact hash"):
        publish_generated_navigation_bundle(
            tmp_path / "bundle",
            artifact=artifact,
            source_map=source_map,
            generated_path=generated,
            root_source_unit="bundle_top.zhl",
            root_source_digest=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )


def test_source_map_requires_complete_source_snapshot_identity(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE, source_unit="bundle_top.zhl")
    artifact, source_map = emit_artifact_with_source_map(
        compilation.ir, selected_ir_identity=compilation.selected_ir_identity
    )
    incomplete = GeneratedSourceMap(
        source_map.backend,
        source_map.module,
        source_map.selected_ir_identity,
        source_map.artifact_hash,
        tuple(
            replace(
                entry,
                source_origin=replace(entry.source_origin, source_unit=None),
            )
            for entry in source_map.entries
        ),
    )
    generated = tmp_path / "BundleTop.sv"
    generated.write_text(artifact.text, encoding="utf-8")

    with pytest.raises(GeneratedNavigationBundleError, match="incomplete"):
        publish_generated_navigation_bundle(
            tmp_path / "bundle",
            artifact=artifact,
            source_map=incomplete,
            generated_path=generated,
            root_source_unit="bundle_top.zhl",
            root_source_digest=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
