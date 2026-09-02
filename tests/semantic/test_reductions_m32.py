import unittest
from pathlib import Path

from zlang.compiler import compile_source
from zlang.reductions import (
    ReductionSearch,
    ReductionTopology,
    expand_reduction,
    extract_best_reduction,
    recognize_reduction,
)
from zlang.costs import UnifiedConstraint
from zlang.ir.expressions import CostMetric


ROOT = Path(__file__).resolve().parents[2]


class ReductionM32Tests(unittest.TestCase):
    def _expr(self, path):
        return compile_source((ROOT / path).read_text()).ir.assignments[0].expression

    def test_dot_and_indexed_sum_normalize_to_same_semantics(self):
        dot = self._expr("examples/dot_builtin.zhl")
        summed = self._expr("examples/dot_product.zhl")
        dot_semantics, _ = recognize_reduction(dot)
        sum_semantics, _ = recognize_reduction(summed)
        self.assertEqual(dot_semantics, sum_semantics)
        self.assertEqual(
            [item.topology for item in expand_reduction(dot)],
            [item.topology for item in expand_reduction(summed)],
        )

    def test_linear_balanced_and_lane_topologies_are_bounded(self):
        candidates = expand_reduction(self._expr("examples/dot_builtin.zhl"), ReductionSearch(max_lane_counts=4))
        self.assertIn(ReductionTopology.LINEAR, [item.topology for item in candidates])
        self.assertIn(ReductionTopology.BALANCED, [item.topology for item in candidates])
        self.assertIn(ReductionTopology.LANE_GROUPED, [item.topology for item in candidates])
        self.assertEqual(len(candidates), len({item.identity for item in candidates}))

    def test_non_power_of_two_sum_is_supported_without_padding(self):
        compilation = compile_source(
            "module M { in a:u3 in b:u3 in c:u3 out y:u5 y=a+b+c }"
        )
        candidates = expand_reduction(compilation.ir.assignments[0].expression)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].semantics.count, 3)

    def test_m28_selection_and_dsp_bound(self):
        candidates = expand_reduction(self._expr("examples/dot_builtin.zhl"))
        selected = extract_best_reduction(candidates, CostMetric.LUT)
        self.assertTrue(selected.selected.legal)
        no_dsp = extract_best_reduction(
            candidates, CostMetric.LUT,
            [UnifiedConstraint(CostMetric.DSP, maximum=0)],
        )
        self.assertEqual(no_dsp.selected.implementation_policy, "generic")

    def test_signed_reduction_is_supported(self):
        compilation = compile_source(
            "module M { in a:vec<3,s3> in b:vec<3,s3> out y:s8 y=dot(a,b) }"
        )
        candidates = expand_reduction(compilation.ir.assignments[0].expression)
        self.assertTrue(candidates)
        self.assertTrue(all(item.timing.ii == 1 for item in candidates))


if __name__ == "__main__":
    unittest.main()
