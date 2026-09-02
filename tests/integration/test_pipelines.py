from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.toolchain import CLASH_ENVIRONMENT, CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.cli import main
from zlang.simulate import simulate_cycles
from zlang.opt import render


ROOT = Path(__file__).resolve().parents[2]


class PipelineIntegrationTests(unittest.TestCase):
    def test_pipelined_mac_has_exact_latency(self) -> None:
        module = compile_source((ROOT / "examples/pipelined_mac.zhl").read_text()).ir
        outputs = simulate_cycles(
            module,
            [
                {"a": 2, "b": 3, "c": 4},
                {"a": 5, "b": 6, "c": 7},
                {"a": 8, "b": 9, "c": 10},
                {"a": 11, "b": 12, "c": 13},
            ],
        )
        self.assertEqual([item["y"] for item in outputs], [0, 0, 10, 37])

    @unittest.skipUnless(CLASH_EXECUTABLE, "Clash executable is not available")
    def test_pipelined_mac_compiles_to_verilog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(
                [
                    CLASH_EXECUTABLE,
                    "--verilog",
                    str(ROOT / "examples/generated/PipelinedMAC.hs"),
                    "-outputdir",
                    temporary,
                ],
                check=True,
                cwd=ROOT,
                env=CLASH_ENVIRONMENT,
            )

    def test_auto_pipeline_report_golden_and_cycle_latency(self) -> None:
        source = (ROOT / "examples/auto_pipeline_products.zhl").read_text()
        result = compile_source(source)
        self.assertEqual(
            result.pipeline_report,
            (ROOT / "examples/generated/AutoPipelineProducts.pipeline").read_text(),
        )
        self.assertEqual(
            render(result.optimization_ir),
            (ROOT / "examples/generated/AutoPipelineProducts.opt").read_text(),
        )
        cycles = [
            {
                "a": value + 1,
                "b": 2,
                "c": value + 2,
                "d": 3,
                "e": value + 3,
                "f": 4,
                "g": value + 4,
                "h": 5,
            }
            for value in range(6)
        ]
        outputs = simulate_cycles(result.ir, cycles)
        self.assertEqual(
            [item["y"] for item in outputs],
            [0, 0, 0, 40, 54, 68],
        )

    def test_cli_writes_the_pipeline_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "AutoPipelineProducts.pipeline"
            clash = Path(temporary) / "AutoPipelineProducts.hs"
            status = main(
                [
                    str(ROOT / "examples/auto_pipeline_products.zhl"),
                    "-o",
                    str(clash),
                    "--pipeline-report",
                    str(report),
                ]
            )
            self.assertEqual(status, 0)
            self.assertEqual(
                report.read_text(),
                (
                    ROOT / "examples/generated/AutoPipelineProducts.pipeline"
                ).read_text(),
            )


if __name__ == "__main__":
    unittest.main()
