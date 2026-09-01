from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from zlang.cli import main
from zlang.compiler import compile_source
from zlang.implementations import select_implementation
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]


class ImplementationChoiceIntegrationTests(unittest.TestCase):
    def test_logic_and_dsp_choices_have_identical_fixed_latency_behavior(self) -> None:
        compilation = compile_source((ROOT / "examples/mac_choice.zl").read_text())
        logic = select_implementation(compilation.ir, "y", "mul_add")
        cycles = [
            {"a": 2, "b": 3, "c": 4},
            {"a": 5, "b": 6, "c": 7},
            {"a": 8, "b": 9, "c": 10},
            {"a": 1, "b": 2, "c": 3},
        ]
        reset = [True, False, False, False]
        expected = [{"y": 0}, {"y": 0}, {"y": 37}, {"y": 82}]

        self.assertEqual(simulate_cycles(compilation.ir, cycles, reset), expected)
        self.assertEqual(simulate_cycles(logic, cycles, reset), expected)

    def test_cli_writes_implementation_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "MacChoice.implementations"
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        str(ROOT / "examples/mac_choice.zl"),
                        "--implementation-report",
                        str(report),
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                report.read_text(),
                (ROOT / "examples/generated/MacChoice.implementations").read_text(),
            )


if __name__ == "__main__":
    unittest.main()
