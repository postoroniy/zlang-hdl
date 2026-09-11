"""Compiler-owned joint M35/M39 planning and evidence coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zlang.candidate_sites import CandidateSiteKind
from zlang.compiler import compile_source
from zlang.evidence_report import (
    EvidenceReportError,
    EvidenceReportPayload,
    render_evidence_json,
)
from zlang.formal_exploration import FormalPolicy
from zlang.formal_orchestration import (
    CompilerFormalExecutionPlan,
    M39AttemptReference,
    collect_m39_evidence,
)
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.verification_bundle import load_verification_bundle
from zlang.verification_publication import (
    publish_compilation_verification_bundle,
)


class _BoundVerifier:
    formal_route = "M36_direct_systemverilog"

    @staticmethod
    def _identity(candidate) -> dict[str, str]:
        suffix = candidate.implementation_identity
        return {
            "property_identity": f"m36.joint-plan.{suffix}",
            "reference_artifact_hash": "a" * 64,
            "implementation_artifact_hash": "b" * 64,
            "artifact_hash": "b" * 64,
            "harness_hash": "c" * 64,
            "backend_identity": "d" * 64,
            "assumptions_identity": "e" * 64,
        }

    def cache_identity(self, candidate, _config):
        return self._identity(candidate)

    def __call__(self, candidate, config):
        return {
            "status": FormalStatus.BOUNDED_PASS,
            "mode": ProofMode.BMC,
            "depth": config.bmc_depth,
            "backend": "direct_systemverilog",
            "engine": "sby",
            "solver": "z3",
            **self._identity(candidate),
        }


DOUBLE_SITE_SOURCE = """
module DoubleSite {
    clock clk reset rst
    in a : u8
    out y : u8
    out z : u8
    y = implement { a ^ 0 intent { minimize lut } }
    z = implement { a ^ 0 intent { minimize lut } }
    assert same @ clk { y == z }
}
"""


def _compiler_plan(directory: Path) -> CompilerFormalExecutionPlan:
    loaded = load_verification_bundle(directory)
    payload = loaded.verification_ir["payload"]
    return CompilerFormalExecutionPlan.from_data(
        payload["compiler_execution_plan"]
    )


def test_required_proven_counterexample_is_a_valid_m39_attempt() -> None:
    attempt = M39AttemptReference(
        "candidate-site:test",
        "candidate:test",
        1,
        "evidence:test",
        FormalPolicy.REQUIRED_PROVEN,
        FormalStatus.FAILED.value,
        ProofMode.PROVE.value,
        8,
        "m36.test",
        "M36_direct_systemverilog",
    )

    assert M39AttemptReference.from_data(attempt.to_data()) == attempt


def test_duplicate_candidate_implementations_are_linked_by_exact_site(
    tmp_path: Path,
) -> None:
    compilation = compile_source(
        DOUBLE_SITE_SOURCE,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=_BoundVerifier(),
    )
    publish_compilation_verification_bundle(compilation, tmp_path / "bundle")
    plan = _compiler_plan(tmp_path / "bundle")
    evidence = collect_m39_evidence(compilation)

    # Both outputs deliberately retain identical implementations.  Their
    # semantic sites, and consequently their evidence records, stay distinct.
    candidate_sites: dict[str, set[str]] = {}
    for site in plan.candidate_site_ledger.sites:
        for candidate in site.candidates:
            candidate_sites.setdefault(candidate.candidate_identity, set()).add(
                site.identity
            )
    assert any(len(sites) > 1 for sites in candidate_sites.values())
    assert len(plan.m39_attempts) == len(evidence)
    assert {item.evidence_id for item in plan.m39_attempts} == {
        item.evidence_id for item in evidence
    }
    assert all(
        dict(item.details)["site_identity"]
        == next(
            attempt.site_identity
            for attempt in plan.m39_attempts
            if attempt.evidence_id == item.evidence_id
        )
        for item in evidence
    )

    restored = CompilerFormalExecutionPlan.from_json(plan.to_json())
    assert restored == plan
    report = EvidenceReportPayload.from_json(
        render_evidence_json(evidence, formal_execution_plan=plan)
    )
    assert report.formal_execution_plan == plan
    assert report.evidence == tuple(sorted(evidence, key=lambda item: item.evidence_id))


def test_formal_policy_off_retains_ledger_but_has_no_m39_attempts(
    tmp_path: Path,
) -> None:
    compilation = compile_source(
        DOUBLE_SITE_SOURCE,
        formal_policy=FormalPolicy.OFF,
    )
    assert compilation.candidate_site_ledger is not None
    assert compilation.candidate_site_ledger.sites
    assert collect_m39_evidence(compilation) == ()

    publish_compilation_verification_bundle(compilation, tmp_path / "bundle")
    plan = _compiler_plan(tmp_path / "bundle")
    assert plan.formal_policy is FormalPolicy.OFF
    assert plan.candidate_site_ledger.sites
    assert plan.m39_attempts == ()
    assert EvidenceReportPayload.from_json(
        render_evidence_json((), formal_execution_plan=plan)
    ).formal_execution_plan == plan


def test_common_evidence_json_rejects_missing_or_corrupted_plan_links(
    tmp_path: Path,
) -> None:
    compilation = compile_source(
        DOUBLE_SITE_SOURCE,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=_BoundVerifier(),
    )
    publish_compilation_verification_bundle(compilation, tmp_path / "bundle")
    plan = _compiler_plan(tmp_path / "bundle")
    evidence = collect_m39_evidence(compilation)
    payload = json.loads(
        render_evidence_json(evidence, formal_execution_plan=plan)
    )

    missing = dict(payload)
    missing["evidence"] = missing["evidence"][1:]
    with pytest.raises(EvidenceReportError, match="differ from compiler plan"):
        EvidenceReportPayload.from_data(missing)

    unknown = dict(payload)
    unknown["unexpected"] = True
    with pytest.raises(EvidenceReportError, match="fields differ"):
        EvidenceReportPayload.from_data(unknown)

    corrupted = json.loads(json.dumps(payload))
    corrupted["evidence"][0]["details"] = [
        pair
        for pair in corrupted["evidence"][0]["details"]
        if pair[0] != "site_identity"
    ]
    with pytest.raises(EvidenceReportError, match="differs from compiler plan"):
        EvidenceReportPayload.from_data(corrupted)


@pytest.mark.parametrize(
    ("source", "top", "required_kind"),
    (
        (
            Path("examples/cost_mac.zhl").read_text(encoding="utf-8"),
            None,
            CandidateSiteKind.CHOICE_AUTO,
        ),
        (
            Path("examples/implementation_intent.zhl").read_text(encoding="utf-8"),
            "FirArchitecture",
            CandidateSiteKind.IMPLEMENT,
        ),
        (
            "module Child { in a:u8 out y:u8 "
            "y=implement { a ^ 0 intent { minimize lut } } } "
            "module Parent { in a:u8 out y:u8 child:Child { a=a } y=child.y }",
            "Parent",
            CandidateSiteKind.IMPLEMENT,
        ),
    ),
)
def test_common_m39_collection_covers_choice_implement_and_nested_sites(
    source: str,
    top: str | None,
    required_kind: CandidateSiteKind,
) -> None:
    compilation = compile_source(
        source,
        top=top,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=_BoundVerifier(),
    )
    evidence = collect_m39_evidence(compilation)
    assert evidence
    assert required_kind in {
        site.kind for site in compilation.candidate_site_ledger.sites
    }
    site_ids = {site.identity for site in compilation.candidate_site_ledger.sites}
    assert all(dict(item.details)["site_identity"] in site_ids for item in evidence)
    assert [item.evidence_id for item in evidence] == sorted(
        item.evidence_id for item in evidence
    )
