"""Real M36/M38 validation for one pure combinational scalar child."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.compiler import compile_source
from zlang.cross_backend import run_cross_backend_formal, validate_artifacts
from zlang.equivalence import emit_miter_with_metadata, run_equivalence_formal
from zlang.formal_artifact_provider import FormalArtifactProvider
from zlang.ir.cross_backend import CrossBackendStatus
from zlang.ir.equivalence import BindingMap, EquivalenceStatus
from zlang.root_equivalence import (
    RootEquivalenceError,
    RootEquivalencePlan,
    execute_root_equivalence,
    prepare_root_equivalence,
)
from zlang.toolchain import find_clash_executable
from zlang.triangular_evidence import M38EvidenceClassification


SOURCE = """
module Child {
    in a : u8
    in b : u8
    out sum : u9
    sum = a + b
}

module Top {
    in a : u8
    in b : u8
    in bias : u9
    out y : u11
    inst child : Child { a b }
    y = extend<10>(child.sum) + extend<10>(bias)
}
"""


def _tools() -> tuple[str, str]:
    clash = find_clash_executable()
    missing = tuple(
        item
        for item in ("yosys", "sby", "yosys-smtbmc", "z3", "verilator")
        if shutil.which(item) is None
    )
    if clash is None or missing:
        pytest.skip(
            "root hierarchy equivalence requires Clash and formal/RTL tools: "
            + ("Clash " if clash is None else "")
            + " ".join(missing)
        )
    return clash, shutil.which("yosys") or "yosys"


def _mutate_child_arithmetic(artifact: BackendArtifact) -> BackendArtifact:
    # Both formal-only artifacts are Yosys-normalized, so this is the exact
    # widened Child ``a + b`` operation rather than the parent's final add.
    # Clash may retain harmless duplicate child expressions; mutate every copy
    # so the output-reachable copy cannot remain correct by accident.
    child_add = re.compile(
        r"\{ 1'h0, (?P<a>(?:a|\\child\.a\s+)) \} \+ "
        r"\{ 1'h0, (?P<b>(?:b|\\child\.b\s+)) \}"
    )
    text, count = child_add.subn(
        lambda match: (
            f"{{ 1'h0, {match.group('a')} }} - "
            f"{{ 1'h0, {match.group('b')} }}"
        ),
        artifact.text,
    )
    assert count >= 1
    digest = hashlib.sha256(text.encode()).hexdigest()
    return replace(
        artifact,
        text=text,
        artifact_hash=digest,
        formal_artifact_hash=digest,
        bindings=tuple(
            replace(item, artifact_hash=digest) for item in artifact.bindings
        ),
    )


def _run_mutated_m36(prepared, artifact: BackendArtifact, work: Path):
    binding_map = BindingMap(
        (*prepared.reference_artifact.bindings, *artifact.bindings),
        map_version=max(
            prepared.reference_artifact.manifest_version,
            artifact.manifest_version,
        ),
    )
    emission = emit_miter_with_metadata(
        prepared.property,
        binding_map,
        reference_module=prepared.reference_artifact.module,
        implementation_module=artifact.module,
    )
    return run_equivalence_formal(
        prepared.property,
        "\n".join(
            (prepared.reference_artifact.text, artifact.text, emission.source)
        ),
        top="m36_" + prepared.property.id.replace(".", "_"),
        backend=artifact.backend,
        depth=4,
        reference_hash=prepared.reference_artifact.artifact_hash,
        implementation_hash=artifact.artifact_hash,
        trace_metadata=emission.trace_metadata,
        work_directory=work,
    )


def test_root_plan_artifacts_and_real_triangle_are_deterministic(
    tmp_path: Path,
) -> None:
    clash, yosys = _tools()
    module = compile_source(SOURCE, top="Top", include_clash=False).ir
    provider = FormalArtifactProvider()
    first = prepare_root_equivalence(
        module,
        "y",
        clash_executable=clash,
        yosys_executable=yosys,
        artifact_provider=provider,
    )
    second = prepare_root_equivalence(
        module,
        "y",
        clash_executable=clash,
        yosys_executable=yosys,
        artifact_provider=provider,
    )

    assert RootEquivalencePlan.from_data(
        json.loads(json.dumps(first.plan.to_data(), sort_keys=True))
    ) == first.plan
    corrupted = first.plan.to_data()
    corrupted["reference_identity"] = "0" * 64
    with pytest.raises(RootEquivalenceError, match="reference identity mismatch"):
        RootEquivalencePlan.from_data(corrupted)
    assert second.plan == first.plan
    assert second.clash.implementation_artifact == first.clash.implementation_artifact
    assert (
        second.direct_systemverilog.implementation_artifact
        == first.direct_systemverilog.implementation_artifact
    )
    clash_artifact = first.clash.implementation_artifact
    direct_artifact = first.direct_systemverilog.implementation_artifact
    assert clash_artifact is not None and direct_artifact is not None
    assert clash_artifact.module != direct_artifact.module
    assert clash_artifact.module.startswith("ZLangRootEq_")
    assert direct_artifact.module.startswith("ZLangRootEq_")
    assert "module Top" not in clash_artifact.text
    assert "module Top" not in direct_artifact.text
    assert "module Child" not in direct_artifact.text
    validate_artifacts(clash_artifact, direct_artifact, first.m38_property)
    assert provider.stats.memory_hits >= 3

    for artifact in (clash_artifact, direct_artifact):
        rtl = tmp_path / f"{artifact.module}.sv"
        rtl.write_text(artifact.text)
        completed = subprocess.run(
            (
                "verilator",
                "--lint-only",
                "-Wall",
                "-Wno-DECLFILENAME",
                "-Wno-UNUSEDSIGNAL",
                "--top-module",
                artifact.module,
                str(rtl),
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout

    result = execute_root_equivalence(
        first,
        depth=4,
        work_directory=tmp_path / "formal",
        artifact_provider=provider,
    )
    assert result.clash_m36.status is EquivalenceStatus.BOUNDED_PASS
    assert result.direct_systemverilog_m36.status is EquivalenceStatus.BOUNDED_PASS
    assert result.m38 is not None
    assert result.m38.status is CrossBackendStatus.BOUNDED_PASS
    assert result.triangle.classification is M38EvidenceClassification.TRIANGULAR


def test_mutating_either_derived_backend_fails_its_m36_and_m38(
    tmp_path: Path,
) -> None:
    clash, yosys = _tools()
    module = compile_source(SOURCE, top="Top", include_clash=False).ir
    prepared = prepare_root_equivalence(
        module,
        "y",
        clash_executable=clash,
        yosys_executable=yosys,
    )
    clash_artifact = prepared.clash.implementation_artifact
    direct_artifact = prepared.direct_systemverilog.implementation_artifact
    assert clash_artifact is not None and direct_artifact is not None

    for label, original, other in (
        ("clash", clash_artifact, direct_artifact),
        ("direct", direct_artifact, clash_artifact),
    ):
        mutated = _mutate_child_arithmetic(original)
        m36 = _run_mutated_m36(
            prepared, mutated, tmp_path / f"{label}-m36"
        )
        assert m36.status is EquivalenceStatus.FAILED
        assert m36.counterexample is not None
        assert m36.source_origin == prepared.materialized.expression.origin

        left, right = (
            (mutated, other) if label == "clash" else (other, mutated)
        )
        m38 = run_cross_backend_formal(
            prepared.m38_property,
            left,
            right,
            inputs=prepared.clash.input_semantic_ids,
            depth=4,
            work_directory=tmp_path / f"{label}-m38",
        )
        assert m38.status is CrossBackendStatus.FAILED
        assert m38.counterexample is not None
        assert m38.source_origin == prepared.materialized.expression.origin
