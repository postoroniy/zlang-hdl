"""Selection-owned M39 candidate-site ledger coverage."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from zlang.candidate_sites import (
    CandidateSiteError,
    CandidateSiteKind,
    CandidateSiteLedger,
    build_candidate_site_ledger,
    candidate_formal_records,
    pipeline_site_key,
)
from zlang.compilation_session import (
    CompilationSession,
    _restore_selection_formal_records,
)
from zlang.compiler import compile_source
from zlang.parser import ParseError
from zlang.costs import CostExtractionError
from zlang.exploration import TransformFamily
from zlang.formal_exploration import FormalPolicy
from zlang.formal_exploration import FormalExplorationConfig
from zlang.formal_candidate import gate_standalone_pipelines
from zlang.implementation_request import (
    ImplementationContribution,
    ImplementationObjective,
    ObjectiveDirection,
    PolicyOrigin,
    TransformPolicy,
)
from zlang.ir import expressions as expr
from zlang.ir.equivalence import EquivalenceCounterexample
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.opt import canonical_ir_identity, lower, restore
from zlang.simulate import simulate


class _BoundVerifier:
    formal_route = "M36_clash"

    def __init__(self, callback):
        self.callback = callback

    @staticmethod
    def _identity(candidate) -> dict[str, str]:
        suffix = candidate.implementation_identity
        return {
            "property_identity": f"m36.candidate-ledger.{suffix}",
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
        result = dict(self.callback(candidate, config))
        identity = self._identity(candidate)
        result.setdefault("mode", ProofMode.BMC)
        result.setdefault("depth", config.bmc_depth)
        result.setdefault("engine", config.engine)
        result.setdefault("solver", config.solver)
        result.setdefault("backend", "clash")
        for key, value in identity.items():
            result.setdefault(key, value)
        if (
            FormalStatus(result["status"]) is FormalStatus.FAILED
            and result.get("counterexample") is None
        ):
            result["counterexample"] = EquivalenceCounterexample(
                identity["property_identity"],
                failure_cycle=1,
                values=(("mutation", "1'b1"),),
                raw_trace="candidate-ledger rank mutation",
            )
        return result


_SPECIALIZED_CHILD_EXPLORE = """
module Child<P=1> {
    in a : u8
    out y : u8
    y = implement { a ^ P intent { minimize lut } }
}

module Top {
    in a : u8
    out y1 : u8
    out y2 : u8
    c1 : Child<P=1> { a=a }
    c2 : Child<P=2> { a=a }
    y1 = c1.y
    y2 = c2.y
}
"""


def test_m39_rewrite_uses_exact_child_specialization_owner() -> None:
    session = CompilationSession(
        _SPECIALIZED_CHILD_EXPLORE,
        top="Top",
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=_BoundVerifier(
            lambda _candidate, _config: {"status": FormalStatus.BOUNDED_PASS}
        ),
        include_clash=False,
    )

    semantic_sites = tuple(
        item for item in session.semantic_candidate_site_ledger.sites
        if item.output == "y"
    )
    assert len(semantic_sites) == 2
    assert len({item.owner_identity for item in semantic_sites}) == 2

    selected = session.selected_ir
    assert simulate(selected, a=0) == {"y1": 1, "y2": 2}
    assert simulate(selected, a=0x5A) == {"y1": 0x5B, "y2": 0x58}

    repeated = CompilationSession(
        _SPECIALIZED_CHILD_EXPLORE,
        top="Top",
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=_BoundVerifier(
            lambda _candidate, _config: {"status": FormalStatus.BOUNDED_PASS}
        ),
        include_clash=False,
    )
    assert repeated.candidate_site_ledger == session.candidate_site_ledger
    assert canonical_ir_identity(lower(repeated.selected_ir)) == canonical_ir_identity(
        lower(selected)
    )


def test_selection_record_restore_keys_sibling_children_by_specialization() -> None:
    source = """
    module Child<P=1> {
        clock clk reset rst
        in a,b,c,d,e,f,g,h : u8
        out y : u19
        y = implement {
            a*b + c*d + e*f + g*h
            intent { latency<=3 ii==1 dsp<=4 fmax>=100 }
        }
    }
    module Top {
        clock clk reset rst
        in a,b,c,d,e,f,g,h : u8
        out y1,y2 : u19
        c1 : Child<P=1> { a=a b=b c=c d=d e=e f=f g=g h=h }
        c2 : Child<P=2> { a=a b=b c=c d=d e=e f=f g=g h=h }
        y1=c1.y y2=c2.y
    }
    """
    base = compile_source(source, top="Top", include_clash=False).ir
    marked_children = tuple(
        replace(
            child,
            pipeline_explorations=(
                replace(
                    child.pipeline_explorations[0],
                    formal_records=(f"P={child.parameters[0][2]}",),
                ),
            ),
        )
        for child in base.children
    )
    reference = replace(base, children=marked_children)
    restored = restore(lower(base))
    reconnected = _restore_selection_formal_records(reference, restored)

    assert tuple(
        child.pipeline_explorations[0].formal_records
        for child in reconnected.children
    ) == (("P=1",), ("P=2",))


def test_identical_child_specializations_share_one_candidate_site() -> None:
    source = _SPECIALIZED_CHILD_EXPLORE.replace(
        "c2 : Child<P=2>", "c2 : Child<P=1>"
    )
    result = compile_source(source, top="Top", include_clash=False)
    sites = tuple(
        item for item in result.candidate_site_ledger.sites
        if item.output == "y"
    )
    assert len(sites) == 1


def test_semantic_ledger_preserves_detailed_choice_constraint_diagnostic() -> None:
    session = CompilationSession(
        "module Impossible { in a:u8 in b:u8 in c:u16 out y:u17 "
        "y=choice(auto,minimize=lut,lut<=10,dsp<=0){"
        "mul_add=>a*b+c dsp_mac=>a*b+c} }",
        include_clash=False,
    )
    with pytest.raises(
        CostExtractionError,
        match=(
            r"no legal implementation.*dsp_mac violates lut=17 > 10, dsp=1 > 0; "
            r"mul_add violates lut=81 > 10"
        ),
    ):
        _ = session.semantic_candidate_site_ledger


def test_semantic_catalog_never_calls_verifier_and_selection_uses_exact_rank() -> None:
    calls: list[str] = []

    def verify(candidate, _config):
        calls.append(candidate.implementation_identity)
        return {
            "status": (
                FormalStatus.FAILED if len(calls) == 1
                else FormalStatus.BOUNDED_PASS
            )
        }

    session = CompilationSession(
        "module Ranked { in a:u8 out y:u8 "
        "y=implement { a ^ 0 intent { minimize lut } } }",
        formal_policy=FormalPolicy.REQUIRED_BMC,
        formal_verifier=_BoundVerifier(verify),
        include_clash=False,
    )
    assert session.semantic_ir.name == "Ranked"
    static = session.semantic_candidate_site_ledger
    assert calls == []
    assert len(static.sites) == 1
    expected_order = tuple(
        item.candidate_identity for item in static.sites[0].candidates
    )

    assert session.selected_ir.name == "Ranked"
    assert tuple(calls) == expected_order[:2]
    selected = session.candidate_site_ledger.sites[0]
    assert selected.selected_candidate_identity == expected_order[1]
    assert [item.rank for item in selected.candidates] == [1, 2]


def test_scalar_explore_is_not_a_callable_expression() -> None:
    with pytest.raises(ParseError, match="scalar explore was removed"):
        CompilationSession(
            "fn select<type T>(x:T) { explore { x ^ 0 } } "
            "module Nested { in x:u8 out y:u8 y=select(x) }",
            include_clash=False,
        ).semantic_ir


@pytest.mark.parametrize(
    ("filename", "kind"),
    (
        ("cost_mac.zhl", CandidateSiteKind.CHOICE_AUTO),
        ("implementation_intent.zhl", CandidateSiteKind.IMPLEMENT),
        ("elastic_pipeline_auto.zhl", CandidateSiteKind.ELASTIC_PIPELINE),
    ),
)
def test_frozen_source_entry_points_have_deterministic_typed_records(
    filename: str,
    kind: CandidateSiteKind,
) -> None:
    source = (Path("examples") / filename).read_text(encoding="utf-8")
    result = compile_source(source, include_clash=False)
    ledger = result.candidate_site_ledger
    assert ledger is not None
    assert kind in {item.kind for item in ledger.sites}
    assert CandidateSiteLedger.from_json(ledger.to_json()) == ledger
    assert [item.identity for item in ledger.sites] == sorted(
        item.identity for item in ledger.sites
    )


def test_external_profile_region_is_catalogued_without_semantic_verifier() -> None:
    contribution = ImplementationContribution(
        PolicyOrigin("candidate-ledger test profile"),
        transforms=TransformPolicy((TransformFamily.DSP,)),
        objective=ImplementationObjective(
            ObjectiveDirection.MINIMIZE,
            expr.CostMetric.LUT,
        ),
    )
    result = compile_source(
        "module Profiled { in a:u4 in b:u4 in c:u8 out y:u9 y=a*b+c }",
        include_clash=False,
        implementation_contributions=(contribution,),
    )
    ledger = result.candidate_site_ledger
    assert ledger is not None
    assert [item.kind for item in ledger.sites] == [
        CandidateSiteKind.EXTERNAL_PROFILE
    ]


def test_external_profile_is_generated_then_gated_in_selection_rank_order() -> None:
    calls: list[str] = []
    contribution = ImplementationContribution(
        PolicyOrigin("candidate-ledger required profile"),
        transforms=TransformPolicy((TransformFamily.DSP,)),
        objective=ImplementationObjective(
            ObjectiveDirection.MINIMIZE,
            expr.CostMetric.LUT,
        ),
    )

    def verify(candidate, _config):
        calls.append(candidate.implementation_identity)
        return {
            "status": (
                FormalStatus.FAILED if len(calls) == 1
                else FormalStatus.BOUNDED_PASS
            )
        }

    session = CompilationSession(
        "module Profiled { in a:u4 in b:u4 in c:u8 out y:u9 y=a*b+c }",
        include_clash=False,
        implementation_contributions=(contribution,),
        formal_policy=FormalPolicy.REQUIRED_BMC,
        formal_verifier=_BoundVerifier(verify),
    )
    assert session.semantic_candidate_site_ledger.sites == ()
    assert calls == []
    _ = session.selected_ir
    site = next(
        item for item in session.candidate_site_ledger.sites
        if item.kind is CandidateSiteKind.EXTERNAL_PROFILE
    )
    ranked = tuple(item.candidate_identity for item in site.candidates)
    assert tuple(calls) == ranked[:2]
    assert site.selected_candidate_identity == ranked[1]


@pytest.mark.parametrize(
    ("filename", "top", "kind"),
    (
        ("cost_mac.zhl", None, CandidateSiteKind.CHOICE_AUTO),
        ("implementation_intent.zhl", "FirArchitecture", CandidateSiteKind.IMPLEMENT),
    ),
)
def test_required_policy_gates_choice_and_architecture_in_exact_rank_order(
    filename: str,
    top: str | None,
    kind: CandidateSiteKind,
) -> None:
    calls: list[str] = []

    def verify(candidate, _config):
        calls.append(candidate.implementation_identity)
        return {
            "status": (
                FormalStatus.FAILED
                if kind is CandidateSiteKind.CHOICE_AUTO and len(calls) == 1
                else FormalStatus.BOUNDED_PASS
            )
        }

    session = CompilationSession(
        (Path("examples") / filename).read_text(encoding="utf-8"),
        top=top,
        include_clash=False,
        formal_policy=FormalPolicy.REQUIRED_BMC,
        formal_verifier=_BoundVerifier(verify),
    )
    static = next(
        item for item in session.semantic_candidate_site_ledger.sites
        if item.kind is kind
    )
    expected = tuple(item.candidate_identity for item in static.candidates)
    # Formal-aware selection is lazy: a passing first-ranked implement
    # candidate stops the search.  The explicit choice test below retains its
    # two-candidate mutation exercise.
    expected_attempts = (
        expected[:2] if kind is CandidateSiteKind.CHOICE_AUTO else expected[:1]
    )
    assert calls == []
    selected = session.selected_ir
    assert tuple(calls) == expected_attempts
    final = next(
        item for item in session.candidate_site_ledger.sites
        if item.kind is kind
    )
    assert final.selected_candidate_identity == expected_attempts[-1]
    if kind is CandidateSiteKind.CHOICE_AUTO:
        choice = next(
            item.expression for item in selected.assignments
            if isinstance(item.expression, expr.ImplementationChoice)
        )
        assert len(choice.formal_records) == len(expected_attempts)
    else:
        assert len(candidate_formal_records(selected, session._selection.exploration_results)) >= len(expected_attempts)
    assert len(
        candidate_formal_records(selected, session._selection.exploration_results)
    ) >= len(expected_attempts)


def test_candidate_ledger_rejects_corrupted_identity_and_rank() -> None:
    result = compile_source(
        "module E { in a:u8 out y:u8 y=implement { a ^ 0 intent { minimize lut } } }",
        include_clash=False,
    )
    ledger = result.candidate_site_ledger
    assert ledger is not None
    payload = json.loads(ledger.to_json())
    payload["sites"][0]["candidates"][0]["rank"] = 2
    with pytest.raises(CandidateSiteError, match="ranks must be contiguous"):
        CandidateSiteLedger.from_data(payload)


def test_candidate_ledger_identity_is_origin_insensitive() -> None:
    source = (
        "module OriginStable { in a:u8 out y:u8 "
        "y=implement { a ^ 0 intent { minimize lut } } }"
    )
    left = compile_source(source, include_clash=False).candidate_site_ledger
    right = compile_source("\n" + source, include_clash=False).candidate_site_ledger
    assert left is not None and right is not None
    assert left.identity == right.identity
    assert left.sites[0].source_origin != right.sites[0].source_origin
    assert CandidateSiteLedger.from_json(left.to_json()) == left


def test_m39_origin_stripping_preserves_execution_and_evidence_identity() -> None:
    source = (
        "module OriginFormal { in a:u8 out y:u8 "
        "y=implement { a ^ 0 intent { minimize lut } } }"
    )

    def run(text: str):
        calls: list[str] = []

        def verify(candidate, _config):
            calls.append(candidate.implementation_identity)
            return {"status": FormalStatus.BOUNDED_PASS}

        result = CompilationSession(
            text,
            include_clash=False,
            formal_policy=FormalPolicy.REQUIRED_BMC,
            formal_verifier=_BoundVerifier(verify),
        )
        _ = result.selected_ir
        record = result._selection.exploration_results[0].formal_records[0]
        return tuple(calls), record.candidate_identity, result.candidate_site_ledger.identity

    left = run(source)
    right = run("\n" + source)
    assert left == right


def test_pipeline_catalog_classification_ignores_source_origin() -> None:
    """A restored planner record joins its unified site by typed identity."""

    source = Path("examples/implementation_intent.zhl").read_text()
    result = compile_source(source, include_clash=False)
    pipeline = result.ir.pipeline_explorations[0]
    stripped = replace(
        pipeline,
        source_expression=replace(pipeline.source_expression, origin=None),
    )
    restored = replace(result.ir, pipeline_explorations=(stripped,))
    ledger = build_candidate_site_ledger(restored, result.exploration_results)
    assert [site.kind for site in ledger.sites].count(CandidateSiteKind.IMPLEMENT) == 1


def test_mixed_pipeline_catalog_preserves_legacy_tuple_order() -> None:
    source = """
    module MixedCatalog {
      clock clk
      reset rst
      in a,b,c,d,e,f,g,h:u2
      out y1:u7
      out y2:u7
      y1 = implement { a*b+c*d+e*f+g*h intent { latency >= 1 ii == 1 } }
      y2 = implement { a*b+c*d+e*f+g*h intent { latency >= 1 ii == 1 } }
    }
    """
    base = compile_source(source, include_clash=False).ir
    assert tuple(item.output for item in base.pipeline_explorations) == ("y1", "y2")
    canonical = {pipeline_site_key(base, base.pipeline_explorations[0])}
    updated = gate_standalone_pipelines(
        base,
        FormalExplorationConfig(FormalPolicy.AVAILABLE),
        _BoundVerifier(lambda _candidate, _config: {"status": FormalStatus.BOUNDED_PASS}),
        canonical_site_keys=canonical,
    )
    assert tuple(item.output for item in updated.pipeline_explorations) == ("y1", "y2")
    assert updated.pipeline_explorations[0].formal_records == ()
    assert updated.pipeline_explorations[1].formal_records
