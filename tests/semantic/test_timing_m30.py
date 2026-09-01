import unittest

from zlang.compiler import compile_source
from zlang.timing import (
    AlignmentPlan,
    TimedEquivalenceGraph,
    TimingInfo,
    TimingRelationKind,
    align_operands,
    relate_timing,
    timing_info,
)


class TimingM30Tests(unittest.TestCase):
    def _expr(self, source):
        return compile_source(source).ir.assignments[0].expression

    def _timed(self, expression):
        return self._expr(f"module M {{ clock clk reset rst in x:u3 out y:u3 y={expression} }}")

    def test_explicit_delay_and_pipeline_have_timed_relation(self):
        x = self._expr("module M { in x:u3 out y:u3 y=x }")
        d = self._timed("delay<2>(x)")
        p = self._timed("pipeline(2) { x }")
        relation = relate_timing(x, d)
        self.assertEqual(relation.kind, TimingRelationKind.TIMED_EQUIVALENT)
        self.assertEqual((relation.delta, relation.earlier, relation.later), (2, x, d))
        self.assertEqual(relate_timing(x, p).delta, 2)
        self.assertEqual(timing_info(d).latency, 2)

    def test_composition_and_conflict_detection(self):
        x = self._expr("module M { in x:u3 out y:u3 y=x }")
        d1 = self._timed("delay<1>(x)")
        d3 = self._timed("delay<3>(x)")
        graph = TimedEquivalenceGraph()
        first = relate_timing(x, d1)
        second = relate_timing(d1, d3)
        composed = graph.compose(first, second)
        self.assertEqual(composed.delta, 3)
        graph.add(first)
        graph.add(second)
        with self.assertRaises(ValueError):
            graph.add(type(first)(first.kind, first.earlier, first.later, 2, first.proof))

    def test_alignment_targets_latest_operand_without_mutation(self):
        x = self._expr("module M { in x:u3 out y:u3 y=x }")
        d = self._timed("delay<2>(x)")
        plan = align_operands((x, d))
        self.assertEqual(plan, AlignmentPlan(2, (2, 0), "minimum-latency alignment"))

    def test_ii_clock_and_reset_are_separate_compatibility_checks(self):
        self.assertEqual(
            relate_timing(
                self._expr("module M { in x:u3 out y:u3 y=x }"),
                self._expr("module M { in x:u3 out y:u3 y=x }"),
                TimingInfo(0, 1, "a", "r"), TimingInfo(0, 2, "a", "r"),
            ).kind,
            TimingRelationKind.INCOMPATIBLE_II,
        )
        self.assertEqual(
            relate_timing(
                self._expr("module M { in x:u3 out y:u3 y=x }"),
                self._expr("module M { in x:u3 out y:u3 y=x }"),
                TimingInfo(0, 1, "a", "r"), TimingInfo(0, 1, "b", "r"),
            ).kind,
            TimingRelationKind.INCOMPATIBLE_CLOCK,
        )


if __name__ == "__main__":
    unittest.main()
