from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import zlang.build_publication as publication_module
from zlang.backend.manifest import MANIFEST_VERSION
from zlang.build_manifest import (
    BackendBuildRecord,
    BuildManifestError,
    CanonicalIrRef,
    PublishedFile,
    WholeBuildManifest,
)
from zlang.build_publication import (
    PhysicalPublication,
    dependency_records,
    publish_manifest_atomically,
)
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _publication_manifest(
    source: PublishedFile, rtl: PublishedFile,
) -> WholeBuildManifest:
    selected = "selected:" + _digest("selected")
    backend = BackendBuildRecord(
        backend="systemverilog",
        module="Top",
        requirement="required",
        status="selected",
        plan_identity=_digest("plan"),
        selected_ir_identity=selected,
        build_identity=_digest("backend-build"),
        artifact_hash=rtl.content_hash,
        manifest_version=MANIFEST_VERSION,
        files=(rtl,),
    )
    return WholeBuildManifest(
        root_source=source,
        dependency_closure=(),
        high_level_ir=CanonicalIrRef(
            "high_level",
            "high-level:" + _digest("high"),
            CANONICAL_IR_IDENTITY_SCHEMA,
        ),
        selected_ir=CanonicalIrRef(
            "selected", selected, CANONICAL_IR_IDENTITY_SCHEMA,
        ),
        implementation_request_identity=_digest("request"),
        implementation_policy_identity=_digest("policy"),
        profile_identity=None,
        backend_builds=(backend,),
        tool_executions=(),
        reports=(),
        evidence=(),
    )


def test_atomic_publication_removes_manifest_if_output_changes_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "Top.zl"
    rtl_path = tmp_path / "Top.sv"
    source_path.write_text("module Top {}\n")
    rtl_path.write_text("module Top; endmodule\n")
    source = PublishedFile.from_bytes(
        "sources/root.zl", source_path.read_bytes(), kind="zlang_source",
    )
    rtl = PublishedFile.from_bytes(
        "backends/systemverilog/Top.sv",
        rtl_path.read_bytes(),
        kind="direct_systemverilog",
    )
    manifest = _publication_manifest(source, rtl)
    output = tmp_path / "build.json"
    original_validate = publication_module.validate_manifest_file_map
    calls = 0

    def validate_then_mutate(*args, **kwargs):
        nonlocal calls
        calls += 1
        original_validate(*args, **kwargs)
        if calls == 1:
            rtl_path.write_text("module Mutated; endmodule\n")

    monkeypatch.setattr(
        publication_module, "validate_manifest_file_map", validate_then_mutate,
    )
    with pytest.raises(BuildManifestError, match="hash/size mismatch") as caught:
        publish_manifest_atomically(
            manifest,
            output,
            publications=(
                PhysicalPublication(source, source_path),
                PhysicalPublication(rtl, rtl_path),
            ),
        )
    assert calls == 2, str(caught.value)
    assert not output.exists()


def test_dependency_records_include_project_and_stdlib_hashes() -> None:
    records = dependency_records(
        None,
        library_dependencies=(("std.math.complex", _digest("complex")),),
    )
    assert records == (
        PublishedFile.from_content_identity(
            "dependencies/std/math/complex.zl",
            _digest("complex"),
            kind="zlang_stdlib_dependency",
        ),
    )
