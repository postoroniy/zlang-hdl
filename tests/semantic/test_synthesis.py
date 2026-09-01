from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.opt import lower, restore
from zlang.synthesis import (
    SynthesisFeedbackError,
    characterize_with_yosys,
    normalized_candidate_hash,
    render_synthesis_report,
)


ROOT = Path(__file__).resolve().parents[2]


class SynthesisFeedbackSemanticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (ROOT / "examples/cost_mac.zl").read_text()
        self.compilation = compile_source(self.source)

    def test_normalized_candidate_hash_ignores_source_formatting(self) -> None:
        reformatted = compile_source("\n\n" + self.source.replace("    ", " "))

        for kind in expr.ImplementationKind:
            with self.subTest(kind=kind.value):
                self.assertEqual(
                    normalized_candidate_hash(self.compilation.ir, "y", kind),
                    normalized_candidate_hash(reformatted.ir, "y", kind),
                )
        self.assertNotEqual(
            normalized_candidate_hash(
                self.compilation.ir, "y", expr.ImplementationKind.MUL_ADD
            ),
            normalized_candidate_hash(
                self.compilation.ir, "y", expr.ImplementationKind.DSP_MAC
            ),
        )

    def test_measured_feedback_can_change_selection_and_round_trips(self) -> None:
        def fake_measure(*args: object) -> tuple[expr.YosysMeasurement, bool]:
            kind = args[2]
            candidate_hash = args[3]
            assert isinstance(kind, expr.ImplementationKind)
            assert isinstance(candidate_hash, str)
            lut_cells = 40 if kind is expr.ImplementationKind.MUL_ADD else 60
            return (
                expr.YosysMeasurement(
                    candidate_hash,
                    "cache-" + kind.value,
                    "Yosys test",
                    "Clash test",
                    "generic-lut6",
                    (
                        ("flatten", "true"),
                        ("lut_inputs", "6"),
                        ("exclude_flip_flops_from_depth", "true"),
                    ),
                    lut_cells,
                    17,
                    lut_cells + 17,
                    7,
                ),
                False,
            )

        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("zlang.synthesis._is_executable", return_value=True),
                patch(
                    "zlang.synthesis._tool_version",
                    side_effect=("Clash test", "Yosys test"),
                ),
                patch("zlang.synthesis._load_or_measure", side_effect=fake_measure),
            ):
                feedback = characterize_with_yosys(
                    self.compilation.ir,
                    Path(temporary),
                    clash_executable="/fake/clash",
                    yosys_executable="/fake/yosys",
                )

        choice = feedback.module.assignments[0].expression
        self.assertIsInstance(choice, expr.ImplementationChoice)
        self.assertEqual(choice.selected, expr.ImplementationKind.MUL_ADD)
        self.assertEqual(restore(lower(feedback.module)), feedback.module)
        report = render_synthesis_report(feedback)
        self.assertIn("feedback_source=measured", report)
        self.assertIn("objective_source=measured_yosys_lut6", report)

    def test_measured_constraint_failure_does_not_relax_the_bound(self) -> None:
        source = self.source.replace("lut<=200", "lut<=30")
        compilation = compile_source(source)

        def illegal_measure(*args: object) -> tuple[expr.YosysMeasurement, bool]:
            kind = args[2]
            candidate_hash = args[3]
            assert isinstance(kind, expr.ImplementationKind)
            assert isinstance(candidate_hash, str)
            return (
                expr.YosysMeasurement(
                    candidate_hash,
                    "cache-" + kind.value,
                    "Yosys test",
                    "Clash test",
                    "generic-lut6",
                    (
                        ("flatten", "true"),
                        ("lut_inputs", "6"),
                        ("exclude_flip_flops_from_depth", "true"),
                    ),
                    50,
                    17,
                    67,
                    7,
                ),
                False,
            )

        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("zlang.synthesis._is_executable", return_value=True),
                patch(
                    "zlang.synthesis._tool_version",
                    side_effect=("Clash test", "Yosys test"),
                ),
                patch("zlang.synthesis._load_or_measure", side_effect=illegal_measure),
            ):
                with self.assertRaisesRegex(
                    SynthesisFeedbackError,
                    r"no legal measured implementation.*lut\(measured_yosys_lut6\)=50 > 30",
                ):
                    characterize_with_yosys(
                        compilation.ir,
                        Path(temporary),
                        clash_executable="/fake/clash",
                        yosys_executable="/fake/yosys",
                    )


if __name__ == "__main__":
    unittest.main()
