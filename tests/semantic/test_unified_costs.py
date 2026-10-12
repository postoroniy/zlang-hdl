import unittest
from dataclasses import dataclass

from zlang.costs import (
    CandidateCost,
    MetricSource,
    MetricValue,
    SourcePolicy,
    UnifiedConstraint,
    extract_best,
)
from zlang.ir.expressions import CostMetric
from zlang.compiler import compile_source
from zlang.opt import saturate
from zlang.costs import extract_best_eclass


class UnifiedCostExtractionTests(unittest.TestCase):
    @dataclass(frozen=True)
    class _Candidate:
        identity: str
        source_origin: str

    def test_objective_and_deterministic_ties_ignore_insertion_order(self) -> None:
        a = ("a", CandidateCost.estimate(lut=2, dsp=0, structural_cost=2))
        b = ("b", CandidateCost.estimate(lut=2, dsp=0, structural_cost=1))
        self.assertEqual(extract_best([a, b]).selected, "b")
        self.assertEqual(extract_best([b, a]).selected, "b")

    def test_tie_break_uses_candidate_identity_not_source_provenance(self) -> None:
        first = self._Candidate("a", "later source location")
        second = self._Candidate("b", "earlier source location")
        cost = CandidateCost.estimate(lut=1)
        selected = extract_best([(second, cost), (first, cost)]).selected
        self.assertEqual(selected.identity, "a")

    def test_constraints_boundary_and_all_rejected(self) -> None:
        candidate = ("x", CandidateCost.estimate(lut=4))
        self.assertEqual(
            extract_best([candidate], constraints=[UnifiedConstraint(CostMetric.LUT, maximum=4)]).selected,
            "x",
        )
        with self.assertRaises(ValueError):
            extract_best([candidate], constraints=[UnifiedConstraint(CostMetric.LUT, maximum=3)])

    def test_unknown_hard_metric_does_not_pass(self) -> None:
        candidate = ("x", CandidateCost.estimate(lut=1, fmax_est=None))
        with self.assertRaises(ValueError):
            extract_best(
                [candidate],
                constraints=[UnifiedConstraint(CostMetric.FMAX_EST, minimum=400)],
            )

    def test_provenance_and_measured_required(self) -> None:
        estimate = CandidateCost.estimate(lut=1)
        self.assertIs(estimate.lut.source, MetricSource.STRUCTURAL_ESTIMATE)
        with self.assertRaises(ValueError):
            extract_best([("x", estimate)], source_policy=SourcePolicy.MEASURED_REQUIRED)

    def test_measured_preferred_ranks_evidence_before_fmax_value(self) -> None:
        estimate = CandidateCost.estimate(lut=1, fmax_est=776)
        routed = CandidateCost(
            *(MetricValue(value, MetricSource.ROUTED_MEASUREMENT) for value in (
                10, 10, 1, 0, 3, 1, 103,
            )),
            structural_cost=10,
        )
        selected = extract_best(
            (("estimate", estimate), ("routed", routed)),
            objective=CostMetric.FMAX_EST,
            source_policy=SourcePolicy.MEASURED_PREFERRED,
        )
        self.assertEqual(selected.selected, "routed")

        # Preference never bypasses legality.  If the measured cohort cannot
        # meet the hard bound, the legal structural estimate remains the
        # deterministic fallback.
        fallback = extract_best(
            (("estimate", estimate), ("routed", routed)),
            objective=CostMetric.FMAX_EST,
            constraints=(UnifiedConstraint(CostMetric.FMAX_EST, minimum=500),),
            source_policy=SourcePolicy.MEASURED_PREFERRED,
        )
        self.assertEqual(fallback.selected, "estimate")

    def test_minimize_each_supported_resource_dimension(self) -> None:
        candidates = [
            ("wide", CandidateCost.estimate(lut=10, ff=4, dsp=2, latency=2)),
            ("small", CandidateCost.estimate(lut=4, ff=8, dsp=1, latency=1)),
        ]
        self.assertEqual(extract_best(candidates, CostMetric.LUT).selected, "small")
        self.assertEqual(extract_best(candidates, CostMetric.FF).selected, "wide")
        self.assertEqual(extract_best(candidates, CostMetric.DSP).selected, "small")
        self.assertEqual(extract_best(candidates, CostMetric.LATENCY).selected, "small")

    def test_egraph_optimization_eclass_uses_unified_extractor(self) -> None:
        compilation = compile_source("module M { in x:u8 out y:u8 y=x|0 }")
        root = compilation.optimization_ir.assignments[0].expression
        saturated = saturate(compilation.optimization_ir, root)
        selected = extract_best_eclass(saturated).selected
        self.assertEqual(selected.attribute("name"), "x")


if __name__ == "__main__":
    unittest.main()
