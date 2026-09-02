from pathlib import Path
import shutil
import tempfile
import unittest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.cli import main
from zlang.compiler import compile_source
from zlang.opt import render, restore
from zlang.simulate import simulate_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]


class CanonicalMetadataIntegrationTests(unittest.TestCase):
    def test_cli_exposes_both_canonical_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            high = directory / "CostMac.high.opt"
            selected = directory / "CostMac.selected.opt"
            status = main(
                [
                    str(ROOT / "examples/cost_mac.zhl"),
                    "-o",
                    str(directory / "CostMac.hs"),
                    "--high-level-ir",
                    str(high),
                    "--optimization-ir",
                    str(selected),
                ]
            )
            self.assertEqual(status, 0)
            self.assertIn("stage high_level", high.read_text())
            self.assertIn("selected=none", high.read_text())
            self.assertIn("stage selected_architecture", selected.read_text())
            self.assertIn("selected=dsp_mac", selected.read_text())

    def test_metadata_report_and_cycle_behavior_match_goldens(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/metadata_datapath.zhl").read_text()
        )
        self.assertEqual(
            render(compilation.optimization_ir),
            (ROOT / "examples/generated/MetadataDatapath.opt").read_text(),
        )
        cycles = (
            {"samples": [1, 2, 3, 4], "coefficients": [4, 3, 2, 1]},
            {"samples": [2, 2, 2, 2], "coefficients": [1, 1, 1, 1]},
            {"samples": [0, 0, 0, 0], "coefficients": [9, 9, 9, 9]},
        )
        resets = (True, False, False)
        self.assertEqual(
            simulate_cycles(compilation.ir, cycles, resets),
            simulate_cycles(restore(compilation.optimization_ir), cycles, resets),
        )
        self.assertEqual(
            simulate_cycles(compilation.ir, cycles, resets),
            [{"y": 0}, {"y": 0}, {"y": 8}],
        )

    @unittest.skipUnless(
        CLASH_EXECUTABLE and shutil.which("verilator"),
        "Clash and Verilator are required",
    )
    def test_metadata_example_generates_and_lints_verilog(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/metadata_datapath.zhl").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            verilog = generate_verilog(
                compilation.clash,
                compilation.ir.name,
                Path(temporary),
                CLASH_EXECUTABLE,
            )
            lint_with_verilator(verilog, compilation.ir.name)


if __name__ == "__main__":
    unittest.main()
