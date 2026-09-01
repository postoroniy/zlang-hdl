from dataclasses import replace
from pathlib import Path
import unittest

from zlang.ir.architectures import (
    ArchitectureEquivalence,
    FirArchitectureKind,
)
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate


ROOT = Path(__file__).resolve().parents[2]


class ArchitectureSemanticTests(unittest.TestCase):
    def _module(self):
        return analyze(parse((ROOT / "examples/fir_architecture.zl").read_text()))

    def test_search_is_bounded_pruned_and_selects_folded_lanes(self) -> None:
        module = self._module()
        exploration = module.architecture_explorations[0]

        self.assertEqual(exploration.theoretical_candidates, 4)
        self.assertEqual(exploration.search_bound, 3)
        self.assertEqual(exploration.budget_pruned, 1)
        self.assertEqual(exploration.constraint_pruned, 2)
        self.assertEqual(exploration.selected, "folded_p2")
        self.assertIs(
            exploration.selected_candidate.kind,
            FirArchitectureKind.FOLDED,
        )
        self.assertEqual(exploration.selected_candidate.parallelism, 2)
        self.assertEqual(exploration.selected_candidate.add_depth, 2)
        self.assertIs(
            exploration.selected_candidate.equivalence,
            ArchitectureEquivalence.MATHEMATICAL,
        )
        self.assertEqual(
            module.assignments[0].expression,
            exploration.selected_candidate.expression,
        )
        self.assertEqual(restore(lower(module)), module)

    def test_every_explored_topology_is_mathematically_equivalent(self) -> None:
        module = self._module()
        exploration = module.architecture_explorations[0]
        vectors = (
            ([0, 0, 0, 0], [255, 3, 9, 1]),
            ([1, 2, 3, 4], [5, 6, 7, 8]),
            ([255, 255, 255, 255], [255, 255, 255, 255]),
        )

        for candidate in exploration.candidates:
            candidate_module = replace(
                module,
                assignments=(
                    replace(
                        module.assignments[0],
                        expression=candidate.expression,
                    ),
                ),
            )
            for samples, coefficients in vectors:
                expected = sum(
                    sample * coefficient
                    for sample, coefficient in zip(
                        samples, coefficients, strict=True
                    )
                )
                self.assertEqual(
                    simulate(
                        candidate_module,
                        samples=samples,
                        coefficients=coefficients,
                    )["y"],
                    expected,
                    candidate.name,
                )

    def test_architecture_traverses_exact_representation_operands(self) -> None:
        module = analyze(parse("""
            module Auto {
                in raw : bits<8>
                in nested : vec<2,vec<2,u8>>
                in tail : vec<1,u8>
                in b : u8 in c : u8 in d : u8
                out y : u18

                raw_value = bitcast<u8>(concat(raw[7:4], raw[3:0]))
                lanes = concat(reshape<vec<4,u8>>(nested), tail)
                y = architecture(
                    auto, parallelism<=2, depth<=2, candidates<=3
                ) {
                      raw_value * b
                    + lanes[0] * c
                    + lanes[1] * d
                    + lanes[4] * b
                }
            }
        """))
        exploration = module.architecture_explorations[0]

        self.assertEqual(
            [candidate.name for candidate in exploration.candidates],
            ["direct", "transposed", "folded_p2"],
        )
        self.assertEqual(
            exploration.selected_candidate.transformations,
            (
                "preserve_product_order",
                "partition_2_accumulator_lanes",
                "balanced_lane_reduction",
            ),
        )
        rendered = repr(exploration.selected_candidate.expression)
        for node in ("Bitcast", "Concat", "Slice", "VectorConcat", "Reshape"):
            self.assertIn(node, rendered)

    def test_duplicate_bounds_are_rejected(self) -> None:
        source = (ROOT / "examples/fir_architecture.zl").read_text()
        source = source.replace(
            "parallelism<=2", "parallelism<=2, parallelism<=3"
        )
        with self.assertRaisesRegex(SemanticError, "repeats 'parallelism'"):
            analyze(parse(source))

    def test_candidate_bound_keeps_all_three_families_explorable(self) -> None:
        source = (ROOT / "examples/fir_architecture.zl").read_text()
        source = source.replace("candidates<=3", "candidates<=2")
        with self.assertRaisesRegex(SemanticError, "bound must be at least 3"):
            analyze(parse(source))

    def test_hard_candidate_cap_prevents_search_explosion(self) -> None:
        source = (ROOT / "examples/fir_architecture.zl").read_text()
        source = source.replace("candidates<=3", "candidates<=33")
        with self.assertRaisesRegex(SemanticError, "hard maximum of 32"):
            analyze(parse(source))

    def test_impossible_constraints_explain_explored_candidates(self) -> None:
        source = (ROOT / "examples/fir_architecture.zl").read_text()
        source = source.replace("parallelism<=2, depth<=2", "parallelism<=1, depth<=2")
        with self.assertRaisesRegex(
            SemanticError,
            r"no legal FIR architecture.*direct violates.*transposed violates.*"
            r"folded_p2 violates",
        ):
            analyze(parse(source))

    def test_non_fir_shape_is_rejected(self) -> None:
        source = (ROOT / "examples/fir_architecture.zl").read_text()
        source = source.replace(
            "samples[3] * coefficients[3]",
            "samples[3]",
        )
        with self.assertRaisesRegex(
            SemanticError, "accepts only a FIR-like sum"
        ):
            analyze(parse(source))

    def test_architecture_must_be_a_complete_wire_output(self) -> None:
        source = (ROOT / "examples/fir_architecture.zl").read_text()
        source = source.replace("y = architecture", "y = 0 + architecture")
        with self.assertRaisesRegex(
            SemanticError, "allowed only as a complete wire-output assignment"
        ):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
