from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from zlang.build_manifest import (
    BackendBuildRecord,
    BuildManifestError,
    CanonicalIrRef,
    EvidenceRecord,
    PublishedFile,
    ReportRecord,
    ToolExecutionRecord,
    WholeBuildManifest,
    validate_manifest_file_map,
    validate_manifest_files,
    validate_published_file_map,
    validate_published_files,
)
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
from zlang.source import SourceOrigin, SourceSpan


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _origin(line: int, construct: str = "module") -> SourceOrigin:
    return SourceOrigin(
        SourceSpan(line, 1, line, 8),
        construct,
        "src/top.zhl",
        _hash("module Top {}\n"),
    )


def _file(path: str, text: str, kind: str, *, origin: SourceOrigin | None = None) -> PublishedFile:
    return PublishedFile.from_bytes(path, text.encode(), kind=kind, source_origin=origin)


def _manifest(*, reverse: bool = False, origin_line: int = 1) -> WholeBuildManifest:
    source = _file("src/top.zhl", "module Top {}\n", "zlang_source", origin=_origin(origin_line))
    dep_a = _file("deps/a.zhl", "module A {}\n", "zlang_dependency")
    dep_b = _file("deps/b.zhl", "module B {}\n", "zlang_dependency")
    sv = _file("build/systemverilog/Top.sv", "module Top; endmodule\n", "rtl")
    source_map = _file("build/systemverilog/Top.source-map.json", "{}\n", "source_map")
    clash = _file("build/clash/Top.hs", "module Top where\n", "generated_source")
    report_file = _file("reports/formal.json", "{}\n", "report")
    selected = "selected:" + _hash("selected")
    backends = (
        BackendBuildRecord(
            "systemverilog", "Top", "required", "selected", _hash("sv-plan"), selected,
            _hash("sv-build"), sv.content_hash, 4, source_map.content_hash, _hash("sv-graph"),
            (report_file, source_map, sv), (), _origin(origin_line, "module-output"),
        ),
        BackendBuildRecord(
            "clash", "Top", "preferred", "generic_fallback", _hash("clash-plan"), selected,
            _hash("clash-build"), clash.content_hash, 4, None, _hash("clash-graph"),
            (clash,), (), _origin(origin_line, "module-output"),
        ),
    )
    evidence = (
        EvidenceRecord(
            "formal.top", "safety", "bounded_pass", "bmc", 8,
            property_id="m35.top", candidate_identity=_hash("candidate"),
            backend="systemverilog", artifact_hash=sv.content_hash,
            engine="sby", solver="z3", route="m35",
            details=(("family", "register"),), source_origin=_origin(origin_line, "guarantee"),
        ),
        EvidenceRecord("typed.top", "type_check", "typed_legal"),
    )
    report = ReportRecord(
        "formal-report", "formal", "json", report_file.content_hash,
        report_file.logical_path, ("formal.top",), _origin(origin_line, "guarantee"),
    )
    tools = (
        ToolExecutionRecord(
            "verilator-lint", "lint", "verilator", "5.0",
            ("verilator", "--lint-only", "<rtl>"), "passed", 0,
            (sv.logical_path,), source_origin=_origin(origin_line),
        ),
        ToolExecutionRecord(
            "sby-bmc", "formal", "sby", "0.68", ("sby", "-f", "<harness>"),
            "passed", 0, (report_file.logical_path,), "bmc", 8,
        ),
    )
    if reverse:
        backends = tuple(reversed(backends))
        evidence = tuple(reversed(evidence))
        tools = tuple(reversed(tools))
    return WholeBuildManifest(
        source,
        tuple(reversed((dep_a, dep_b))) if reverse else (dep_a, dep_b),
        CanonicalIrRef("high_level", "high-level:" + _hash("high"), CANONICAL_IR_IDENTITY_SCHEMA,
                       _hash("high-content"), _origin(origin_line)),
        CanonicalIrRef(
            "selected",
            selected,
            CANONICAL_IR_IDENTITY_SCHEMA,
            _hash("selected-content"),
            _origin(origin_line),
        ),
        _hash("request"),
        _hash("policy"),
        _hash("profile"),
        backends,
        tools,
        (report,),
        evidence,
        (("target", "generic"), ("project", "fixture")),
    )


def _publish(root: Path, manifest: WholeBuildManifest) -> None:
    contents = {
        "src/top.zhl": "module Top {}\n",
        "deps/a.zhl": "module A {}\n",
        "deps/b.zhl": "module B {}\n",
        "build/systemverilog/Top.sv": "module Top; endmodule\n",
        "build/systemverilog/Top.source-map.json": "{}\n",
        "build/clash/Top.hs": "module Top where\n",
        "reports/formal.json": "{}\n",
    }
    for logical_path, text in contents.items():
        path = root / logical_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_manifest_is_order_independent_and_round_trips_strictly() -> None:
    first = _manifest()
    reordered = _manifest(reverse=True)
    assert first == reordered
    assert first.build_identity == reordered.build_identity
    assert first.to_json() == reordered.to_json()
    restored = WholeBuildManifest.from_json(first.to_json())
    assert restored == first
    assert restored.to_json() == first.to_json()
    assert json.loads(first.to_json())["build_identity"] == first.build_identity


def test_source_attribution_is_serialized_but_not_build_identity() -> None:
    first = _manifest(origin_line=1)
    moved = _manifest(origin_line=20)
    assert first.build_identity == moved.build_identity
    assert first.to_json() != moved.to_json()
    payload = json.loads(moved.to_json())
    assert payload["root_source"]["source_origin"]["span"]["start_line"] == 20
    assert payload["evidence"][0]["source_origin"]["source_unit"] == "src/top.zhl"


def test_manifest_identity_is_json_format_independent_and_detects_tampering() -> None:
    manifest = _manifest()
    payload = json.loads(manifest.to_json())
    compact = json.dumps(payload, separators=(",", ":"))
    assert WholeBuildManifest.from_json(compact).build_identity == manifest.build_identity

    payload["selected_ir"]["identity"] = "selected:" + _hash("tampered")
    with pytest.raises(BuildManifestError, match="identity does not match"):
        WholeBuildManifest.from_json(json.dumps(payload))

    payload = json.loads(manifest.to_json())
    payload["host_wall_time"] = 1.25
    with pytest.raises(BuildManifestError, match="unknown field"):
        WholeBuildManifest.from_json(json.dumps(payload))


@pytest.mark.parametrize("path", ["/tmp/out.sv", "../out.sv", "a/../out.sv", "a\\out.sv", "a/"])
def test_published_paths_are_logical_relative_and_normalized(path: str) -> None:
    with pytest.raises(BuildManifestError, match="path"):
        PublishedFile(path, _hash("x"), "rtl", 1)


def test_file_validation_is_relocatable_and_detects_tampering(tmp_path: Path) -> None:
    manifest = _manifest()
    left = tmp_path / "checkout-a"
    right = tmp_path / "checkout-b"
    _publish(left, manifest)
    _publish(right, manifest)
    validate_manifest_files(manifest, left)
    validate_manifest_files(manifest, right)

    (right / "build/systemverilog/Top.sv").write_text("module Wrong; endmodule\n")
    with pytest.raises(BuildManifestError, match="hash/size mismatch"):
        validate_manifest_files(manifest, right)


def test_content_identity_dependencies_validate_via_explicit_physical_map(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "unrelated-checkout" / "top.zhl"
    dependency_path = tmp_path / "package-cache" / "dep.zhl"
    source_path.parent.mkdir()
    dependency_path.parent.mkdir()
    source_path.write_text("module Top {}\n")
    dependency_path.write_text("module Dep {}\n")
    source = PublishedFile.from_bytes(
        "src/top.zhl", source_path.read_bytes(), kind="zlang_source",
    )
    dependency = PublishedFile.from_content_identity(
        "deps/dep.zhl", _hash("module Dep {}\n"), kind="zlang_dependency",
    )
    assert dependency.size is None
    validate_published_file_map(
        (source, dependency),
        {"src/top.zhl": source_path, "deps/dep.zhl": dependency_path},
    )
    dependency_path.write_text("module Changed {}\n")
    with pytest.raises(BuildManifestError, match="hash/size mismatch"):
        validate_published_file_map(
            (source, dependency),
            {"src/top.zhl": source_path, "deps/dep.zhl": dependency_path},
        )


def test_report_path_is_a_first_class_published_output(tmp_path: Path) -> None:
    manifest = _manifest()
    builds = tuple(
        replace(
            backend,
            files=tuple(
                item for item in backend.files
                if item.logical_path != "reports/formal.json"
            ),
        )
        for backend in manifest.backend_builds
    )
    standalone_report = replace(manifest, backend_builds=builds)
    _publish(tmp_path, standalone_report)
    validate_manifest_files(standalone_report, tmp_path)
    physical = {
        path: tmp_path / path
        for path in {
            standalone_report.root_source.logical_path,
            *(item.logical_path for item in standalone_report.dependency_closure),
            *(item.logical_path for build in standalone_report.backend_builds
              for item in (*build.files, *build.companions)),
            standalone_report.reports[0].logical_path,
        }
        if path is not None
    }
    validate_manifest_file_map(standalone_report, physical)


def test_report_logical_paths_are_unique() -> None:
    manifest = _manifest()
    report = manifest.reports[0]
    with pytest.raises(BuildManifestError, match="duplicate report logical path"):
        replace(
            manifest,
            reports=(
                report,
                replace(report, report_id="second-report"),
            ),
        )


def test_file_validation_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.sv"
    outside.write_text("rtl")
    root = tmp_path / "root"
    root.mkdir()
    (root / "out.sv").symlink_to(outside)
    published = _file("out.sv", "rtl", "rtl")
    with pytest.raises(BuildManifestError, match="escapes"):
        validate_published_files((published,), root)


def test_evidence_statuses_preserve_bounded_and_unbounded_truth() -> None:
    bounded = EvidenceRecord("b", "safety", "bounded_pass", "bmc", 12)
    assert bounded.mode == "bmc" and bounded.depth == 12
    assert EvidenceRecord("p", "safety", "proven", "prove").status == "proven"
    failed = EvidenceRecord(
        "f", "mutation", "failed", "bmc", 4,
        counterexample_digest=_hash("trace"),
    )
    assert failed.counterexample_digest == _hash("trace")

    with pytest.raises(BuildManifestError, match="positive depth"):
        EvidenceRecord("bad", "safety", "bounded_pass", "bmc")
    with pytest.raises(BuildManifestError, match="PROVE"):
        EvidenceRecord("bad", "safety", "proven", "bmc", 8)
    with pytest.raises(BuildManifestError, match="unsupported evidence status"):
        EvidenceRecord("bad", "unbounded proof", "generated")
    with pytest.raises(BuildManifestError, match="not a proof result"):
        EvidenceRecord("bad", "typing", "typed_legal", "bmc", 1)
    with pytest.raises(BuildManifestError, match="exactly one counterexample"):
        EvidenceRecord("bad", "safety", "unknown", counterexample_digest=_hash("trace"))
    with pytest.raises(BuildManifestError, match="exactly one counterexample"):
        EvidenceRecord("bad", "safety", "failed", "bmc", 4)


def test_generated_is_not_a_public_status_and_not_run_never_implies_success() -> None:
    with pytest.raises(BuildManifestError, match="unsupported evidence status"):
        EvidenceRecord("h", "formal_harness", "generated")
    not_run = EvidenceRecord("n", "candidate_check", "not_run", "bmc", 8)
    assert not_run.status == "not_run"
    assert not_run.mode == "bmc" and not_run.depth == 8


def test_cache_state_and_report_rendering_do_not_change_build_identity() -> None:
    manifest = _manifest()
    evidence = manifest.evidence[0]
    cached = replace(evidence, details=(*evidence.details, ("cache_state", "hit")))
    executed = replace(evidence, details=(*evidence.details, ("cache_state", "executed")))
    assert cached.to_data() != executed.to_data()
    assert cached.identity_data() == executed.identity_data()

    report = manifest.reports[0]
    rendered_differently = replace(
        report,
        format="text",
        content_hash=_hash("different rendering"),
        logical_path="reports/formal.txt",
    )
    assert report.identity_hash == rendered_differently.identity_hash
    assert report.to_data() != rendered_differently.to_data()

    # Use standalone report paths so the explicit backend-file hash join does
    # not (correctly) reject the changed rendering before identity comparison.
    builds = tuple(
        replace(
            backend,
            files=tuple(item for item in backend.files
                        if item.logical_path != report.logical_path),
        )
        for backend in manifest.backend_builds
    )
    cached_manifest = replace(
        manifest, backend_builds=builds,
        tool_executions=tuple(
            item for item in manifest.tool_executions
            if item.execution_id != "sby-bmc"
        ),
        evidence=tuple(cached if item.evidence_id == cached.evidence_id else item
                       for item in manifest.evidence),
    )
    rendered_manifest = replace(
        cached_manifest,
        reports=(rendered_differently,),
        evidence=tuple(executed if item.evidence_id == executed.evidence_id else item
                       for item in manifest.evidence),
    )
    assert cached_manifest.build_identity == rendered_manifest.build_identity


def test_backend_record_requires_artifacts_only_for_built_routes() -> None:
    selected = "selected:" + _hash("selected")
    with pytest.raises(BuildManifestError, match="requires build/artifact"):
        BackendBuildRecord(
            "systemverilog", "Top", "required", "selected", _hash("plan"), selected,
        )
    unsupported = BackendBuildRecord(
        "systemverilog", "Top", "preferred", "unsupported", _hash("plan"), selected,
    )
    assert unsupported.artifact_hash is None
    with pytest.raises(BuildManifestError, match="matching status"):
        BackendBuildRecord(
            "clash", "Top", "not_requested", "unsupported", _hash("plan"), selected,
        )


def test_canonical_reference_stage_prefix_and_schema_are_strict() -> None:
    with pytest.raises(BuildManifestError, match="high-level:<sha256>"):
        CanonicalIrRef(
            "high_level", "selected:" + _hash("wrong"), CANONICAL_IR_IDENTITY_SCHEMA,
        )
    with pytest.raises(BuildManifestError, match="unsupported canonical IR"):
        CanonicalIrRef("selected", "selected:" + _hash("value"), "future-schema")


def test_artifact_and_report_hashes_join_published_files() -> None:
    manifest = _manifest()
    backend = manifest.backend_builds[-1]
    with pytest.raises(BuildManifestError, match="artifact hash has no published file"):
        replace(backend, artifact_hash=_hash("unpublished"))
    with pytest.raises(BuildManifestError, match="hash differs"):
        replace(
            manifest,
            reports=(replace(manifest.reports[0], content_hash=_hash("wrong")),),
        )


def test_tool_records_use_normalized_shapes_and_published_outputs() -> None:
    with pytest.raises(BuildManifestError, match="absolute host paths"):
        ToolExecutionRecord(
            "lint", "lint", "verilator", "5", ("verilator", "/tmp/Top.sv"), "passed",
        )
    with pytest.raises(BuildManifestError, match="absolute host paths"):
        ToolExecutionRecord(
            "lint", "lint", "verilator", "5",
            ("verilator", "--output=/tmp/Top.sv"), "passed",
        )
    with pytest.raises(BuildManifestError, match="unpublished outputs"):
        replace(_manifest(), tool_executions=(
            ToolExecutionRecord(
                "bad", "lint", "verilator", "5", ("verilator", "<rtl>"), "passed",
                outputs=("build/missing.sv",),
            ),
        ))
    payload = _manifest().to_json()
    assert "timestamp" not in payload
    assert "wall_time" not in payload
    assert "cache_hit" not in payload


def test_reports_must_join_known_evidence_and_physical_content(tmp_path: Path) -> None:
    manifest = _manifest()
    with pytest.raises(BuildManifestError, match="unknown evidence"):
        replace(manifest, reports=(replace(manifest.reports[0], evidence_ids=("missing",)),))
    missing_report = replace(
        manifest,
        reports=(replace(manifest.reports[0], logical_path="reports/missing.json"),),
    )
    _publish(tmp_path, manifest)
    with pytest.raises(BuildManifestError, match="report is missing"):
        validate_manifest_files(missing_report, tmp_path)


def test_strict_restore_rejects_unknown_nested_fields_and_wrong_scalar_types() -> None:
    payload = json.loads(_manifest().to_json())
    payload["evidence"][0]["surprise"] = True
    with pytest.raises(BuildManifestError, match="unknown field"):
        WholeBuildManifest.from_json(json.dumps(payload))

    payload = json.loads(_manifest().to_json())
    payload["root_source"]["size"] = True
    with pytest.raises(BuildManifestError, match="must be an integer"):
        WholeBuildManifest.from_json(json.dumps(payload))

    payload = json.loads(_manifest().to_json())
    payload["schema_version"] = 2
    with pytest.raises(BuildManifestError, match="unsupported whole-build schema version"):
        WholeBuildManifest.from_json(json.dumps(payload))
