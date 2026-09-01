"""Fail-closed bounds for compiler-selected Clash verification fallback."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import shutil

import pytest

from zlang.backend.clash import emit_formal_artifact as emit_clash_formal_artifact
from zlang.backend.clash import (
    ClashEmissionError,
    finalize_formal_verilog_artifact,
)
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_formal_artifact as emit_systemverilog_formal_artifact,
)
from zlang.compiler import compile_source
from zlang.formal_tooling import ClashToolContext
from zlang.toolchain import find_clash_executable
from zlang.toolchain import generate_verilog
from zlang.verification_bundle import VerificationRunConfig, run_verification_bundle
from zlang.verification_publication import publish_compilation_verification_bundle


SOURCE = """
module ClashFallbackRom {
    clock clk reset rst
    in address : u2
    out y : u8

    rom table : rom<u8,4> {
        read_latency 1
        init generate(i in 0..4) i
    }
    table.read_address = address
    y = table.read_data

    assert bounded_data @ clk { y <= 3 }
}
"""

COUNTER_SOURCE = """
module ClashFallbackCounter {
    clock clk reset rst
    out y : u3
    reg count : u3 = 0
    count <- truncate<3>(count + 1)
    y = count
    assert bounded_count @ clk { count <= 7 }
}
"""

PUBLIC_SOURCE = """
module ClashFallbackPublic {
    clock clk reset rst
    in a : u3
    out y : u3
    y = a
    assert passthrough @ clk { y == a }
}
"""


def _compilation():
    return compile_source(SOURCE, include_clash=False)


def _counter_compilation():
    return compile_source(COUNTER_SOURCE, include_clash=False)


def _public_compilation():
    return compile_source(PUBLIC_SOURCE, include_clash=False)


def test_direct_rom_companions_do_not_require_clash_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _compilation()
    direct = emit_systemverilog_formal_artifact(
        compilation.ir,
        compilation.recursive_formal_design,
    )
    assert direct.companions

    monkeypatch.setattr(
        "zlang.verification_publication._clash_fallback_unavailable_reason",
        lambda *_args, **_kwargs: pytest.fail("direct ROM route used Clash fallback"),
    )
    directory = tmp_path / "bundle"
    manifest = publish_compilation_verification_bundle(
        compilation,
        directory,
    )
    assert manifest.jobs and all(item.executable for item in manifest.jobs)
    companions = tuple(item for item in manifest.files if item.kind == "companion")
    assert len(companions) == 1
    assert companions[0].logical_path.startswith("implementation/companions/")
    assert all(
        companions[0].logical_path in item.source_files for item in manifest.jobs
    )


def test_direct_emission_error_selects_the_same_fail_closed_clash_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _compilation()

    def unsupported(*_args: object, **_kwargs: object):
        raise SystemVerilogEmissionError("synthetic unsupported selected IR")

    monkeypatch.setattr(
        "zlang.verification_publication.emit_formal_artifact",
        unsupported,
    )
    monkeypatch.setattr(
        compilation.formal_tool_resolver,
        "clash_context",
        lambda: ClashToolContext(None, None),
    )
    manifest = publish_compilation_verification_bundle(
        compilation,
        tmp_path / "bundle",
    )
    reason = manifest.jobs[0].reason or ""
    assert (
        "direct-SystemVerilog formal route is unavailable: "
        "synthetic unsupported selected IR"
    ) in reason
    assert "Clash executable was not found" in reason


def test_unavailable_assumption_skips_dependent_goal_instead_of_weakening_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = compile_source("""
module MissingAssumptionBinding {
    clock clk reset rst
    in allow : bit
    out y : bit
    y = allow
    assume legal @ clk disable iff rst { allow }
    guarantee visible @ clk disable iff rst { y == y }
}
""", include_clash=False)
    artifact = emit_systemverilog_formal_artifact(
        compilation.ir, compilation.recursive_formal_design,
    )
    blocked = {
        item.semantic_binding_id
        for item in artifact.recursive_bindings
        if item.local_semantic_id == "port:allow"
    }
    assert blocked
    partial = replace(
        artifact,
        formal_observations=tuple(
            item for item in artifact.formal_observations
            if item.semantic_binding_id not in blocked
        ),
    )
    monkeypatch.setattr(
        "zlang.verification_publication.emit_formal_artifact",
        lambda *_args, **_kwargs: partial,
    )
    monkeypatch.setattr(
        "zlang.verification_publication._try_clash_formal_fallback",
        lambda *_args, **_kwargs: (None, "forced Clash outage"),
    )

    manifest = publish_compilation_verification_bundle(
        compilation, tmp_path / "bundle",
    )
    safety = next(item for item in manifest.jobs if item.kind == "safety")
    assert not safety.executable
    assert "assumption_unavailable" in (safety.reason or "")
    assert safety.assumption_ids
    assert not any(item.kind == "harness" for item in manifest.files)


@pytest.mark.skipif(
    find_clash_executable() is None,
    reason="real Clash is unavailable",
)
def test_direct_and_clash_fallback_goals_share_bundle_without_mixed_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = compile_source("""
module MixedFormalRoutes {
    clock clk reset rst
    in a : bit
    out y : bit
    reg count : u2 = 0
    count <- truncate<2>(count + 1)
    y = a
    assert public_path @ clk { y == a }
    assert state_path @ clk { count <= 3 }
}
""", include_clash=False)
    artifact = emit_systemverilog_formal_artifact(
        compilation.ir, compilation.recursive_formal_design,
    )
    blocked = {
        item.semantic_binding_id
        for item in artifact.recursive_bindings
        if item.local_semantic_id == "register:count"
    }
    assert blocked
    partial = replace(
        artifact,
        formal_observations=tuple(
            item for item in artifact.formal_observations
            if item.semantic_binding_id not in blocked
        ),
    )
    monkeypatch.setattr(
        "zlang.verification_publication.emit_formal_artifact",
        lambda *_args, **_kwargs: partial,
    )

    bundle = tmp_path / "bundle"
    manifest = publish_compilation_verification_bundle(compilation, bundle)
    goal_ids = {
        goal.name: goal.semantic_id
        for scope in compilation.ir.verification_scopes
        for goal in scope.goals
    }
    source_goals = tuple(
        item for item in manifest.jobs
        if item.property_id in {
            goal_ids["public_path"], goal_ids["state_path"],
        }
    )
    assert len(source_goals) == 2
    assert {item.backend for item in source_goals} == {
        "direct_systemverilog", "clash",
    }
    assert len({item.route for item in source_goals}) == 2
    payload = __import__("json").loads(
        (bundle / "verification-ir.json").read_text()
    )["payload"]
    binding_sets = {item["route"]: item for item in payload["binding_sets"]}
    for job in source_goals:
        record = binding_sets[job.route]
        assert record["backend"] == job.backend
        harness = "\n".join(
            (bundle / path).read_text()
            for path in job.source_files
            if path.startswith("harness/")
        )
        own_names = {
            item["rtl_name"] for item in record["bindings"]
        }
        other_names = {
            item["rtl_name"]
            for route, other in binding_sets.items()
            if route != job.route
            for item in other["bindings"]
        }
        assert own_names & set(harness.replace("(", " ").replace(")", " ").split())
        # The two backends may legitimately reuse a public ABI token.  Only
        # backend-exclusive observation names must be absent.
        assert not {
            name for name in other_names - own_names if name in harness
        }


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")),
    reason="real SBY/Yosys/Z3 toolchain is unavailable",
)
def test_direct_rom_bundle_replays_with_real_solver(
    tmp_path: Path,
) -> None:
    compilation = _compilation()
    manifest = publish_compilation_verification_bundle(
        compilation,
        tmp_path / "bundle",
    )
    assert all(item.executable for item in manifest.jobs)
    report = run_verification_bundle(
        tmp_path / "bundle",
        config=VerificationRunConfig(depth=5, timeout_seconds=30),
        work_directory=tmp_path / "work",
    )
    assert report.exit_code == 0
    assert {item.status for item in report.results} == {"bounded_pass"}
    assert all(item.work_directory for item in report.results)


@pytest.mark.skipif(
    find_clash_executable() is None
    or not all(shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")),
    reason="real Clash/SBY/Yosys/Z3 toolchain is unavailable",
)
def test_validated_recursive_clash_artifact_is_immutable_and_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _counter_compilation()

    def unsupported(*_args: object, **_kwargs: object):
        raise SystemVerilogEmissionError("forced direct-route outage")

    monkeypatch.setattr(
        "zlang.verification_publication.emit_formal_artifact",
        unsupported,
    )
    manifest = publish_compilation_verification_bundle(
        compilation,
        tmp_path / "bundle",
    )
    assert manifest.jobs and all(item.executable for item in manifest.jobs)
    implementation = next(
        item for item in manifest.files if item.kind == "implementation"
    )
    text = (tmp_path / "bundle" / implementation.logical_path).read_text()
    assert "module ClashFallbackCounter_formal" in text
    assert "zlang_formal_obs_" in text
    assert "module ClashFallbackCounter where" not in text

    report = run_verification_bundle(
        tmp_path / "bundle",
        config=VerificationRunConfig(depth=5, timeout_seconds=30),
        work_directory=tmp_path / "work",
    )
    assert report.exit_code == 0
    assert {item.status for item in report.results} == {"bounded_pass"}
    assert all(item.tool_versions for item in report.results)


@pytest.mark.skipif(
    find_clash_executable() is None
    or not all(shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")),
    reason="real Clash/SBY/Yosys/Z3 toolchain is unavailable",
)
def test_public_only_clash_fallback_uses_exact_annotated_ports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _public_compilation()

    def unsupported(*_args: object, **_kwargs: object):
        raise SystemVerilogEmissionError("forced public direct-route outage")

    monkeypatch.setattr(
        "zlang.verification_publication.emit_formal_artifact",
        unsupported,
    )
    manifest = publish_compilation_verification_bundle(
        compilation,
        tmp_path / "bundle",
    )
    assert manifest.jobs and all(item.executable for item in manifest.jobs)
    implementation = next(
        item for item in manifest.files if item.kind == "implementation"
    )
    text = (tmp_path / "bundle" / implementation.logical_path).read_text()
    assert "module ClashFallbackPublic" in text
    assert "module ClashFallbackPublic where" not in text
    report = run_verification_bundle(
        tmp_path / "bundle",
        config=VerificationRunConfig(depth=5, timeout_seconds=30),
        work_directory=tmp_path / "work",
    )
    assert report.exit_code == 0
    assert {item.status for item in report.results} == {"bounded_pass"}


@pytest.mark.skipif(
    find_clash_executable() is None,
    reason="real Clash is unavailable",
)
def test_fresh_session_clash_route_cache_does_not_regenerate_rtl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "formal-cache"

    def unsupported(*_args: object, **_kwargs: object):
        raise SystemVerilogEmissionError("forced public direct-route outage")

    monkeypatch.setattr(
        "zlang.verification_publication.emit_formal_artifact",
        unsupported,
    )
    first = compile_source(
        PUBLIC_SOURCE,
        include_clash=False,
        formal_cache=cache,
        source_unit="tests/fixtures/clash_route_cache.zl",
    )
    first_manifest = publish_compilation_verification_bundle(
        first,
        tmp_path / "first",
    )
    assert first_manifest.jobs
    assert {item.backend for item in first_manifest.jobs} == {"clash"}

    # A second compilation has a distinct session-local provider.  Its only
    # valid reuse path is the complete, hash-validated prepared-route disk
    # codec; invoking the Clash fallback again is a test failure.
    second = compile_source(
        PUBLIC_SOURCE,
        include_clash=False,
        formal_cache=cache,
        source_unit="tests/fixtures/clash_route_cache.zl",
    )
    monkeypatch.setattr(
        "zlang.verification_publication._try_clash_formal_fallback",
        lambda *_args, **_kwargs: pytest.fail(
            "prepared-route disk hit regenerated Clash RTL"
        ),
    )
    second_manifest = publish_compilation_verification_bundle(
        second,
        tmp_path / "second",
    )

    assert second.formal_artifact_provider is not None
    assert second.formal_artifact_provider.stats.disk_hits >= 1
    assert second_manifest.to_json() == first_manifest.to_json()
    assert {
        item.logical_path: (tmp_path / "first" / item.logical_path).read_bytes()
        for item in first_manifest.files
    } == {
        item.logical_path: (tmp_path / "second" / item.logical_path).read_bytes()
        for item in second_manifest.files
    }


@pytest.mark.skipif(
    find_clash_executable() is None,
    reason="real Clash is unavailable",
)
def test_clash_finalizer_rejects_a_wrong_public_port_width(tmp_path: Path) -> None:
    compilation = _counter_compilation()
    artifact = emit_clash_formal_artifact(
        compilation.ir,
        compilation.recursive_formal_design,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    files = generate_verilog(
        artifact.text,
        compilation.ir.name,
        tmp_path / "clash",
        find_clash_executable(),
        companions=artifact.companions,
    )
    finalized = finalize_formal_verilog_artifact(artifact, files)
    repeated = finalize_formal_verilog_artifact(artifact, tuple(reversed(files)))
    assert finalized.text == repeated.text
    assert finalized.artifact_hash == repeated.artifact_hash
    assert finalized.formal_artifact_hash == finalized.artifact_hash
    restored = BackendArtifact.from_json(finalized.to_json())
    assert restored.artifact_hash == finalized.artifact_hash
    assert all(
        item.artifact_hash == finalized.artifact_hash
        for item in restored.bindings
    )
    top = next(path for path in files if path.name == "ClashFallbackCounter_formal.v")
    top.write_text(top.read_text().replace(
        "output wire [2:0] y",
        "output wire [1:0] y",
    ))
    with pytest.raises(ClashEmissionError, match="exactly one validated formal top|public"):
        finalize_formal_verilog_artifact(artifact, files)
