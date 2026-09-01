from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from zlang.cli import main
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]


class CostExtractionIntegrationTests(unittest.TestCase):
    def test_estimated_dsp_and_constrained_logic_are_behaviorally_equal(self) -> None:
        dsp = compile_source((ROOT / "examples/cost_mac.zl").read_text())
        logic = compile_source((ROOT / "examples/cost_mac_no_dsp.zl").read_text())
        cycles = [
            {"a": 2, "b": 3, "c": 4},
            {"a": 5, "b": 6, "c": 7},
            {"a": 8, "b": 9, "c": 10},
            {"a": 1, "b": 2, "c": 3},
        ]
        reset = [True, False, False, False]
        expected = [{"y": 0}, {"y": 0}, {"y": 37}, {"y": 82}]

        self.assertEqual(simulate_cycles(dsp.ir, cycles, reset), expected)
        self.assertEqual(simulate_cycles(logic.ir, cycles, reset), expected)

    def test_cli_writes_explainable_estimate_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "CostMac.costs"
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        str(ROOT / "examples/cost_mac.zl"),
                        "--cost-report",
                        str(report),
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                report.read_text(),
                (ROOT / "examples/generated/CostMac.costs").read_text(),
            )
            self.assertIn("cost_source=estimate measured=false", report.read_text())


if __name__ == "__main__":
    unittest.main()
