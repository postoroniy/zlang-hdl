import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend_comparison import (
    BENCHMARKS,
    COMPARISON_SCHEMA,
    load_benchmark_source,
    render_json,
    run_comparison,
)
from zlang.cli import main


ROOT = Path(__file__).resolve().parents[2]
TOOLS = (
    CLASH_EXECUTABLE,
    shutil.which("verilator"),
    shutil.which("yosys"),
    shutil.which("iverilog"),
    shutil.which("vvp"),
)


class BackendComparisonIntegrationTests(unittest.TestCase):
    def test_installed_comparison_corpus_matches_checkout_examples(self) -> None:
        for benchmark in BENCHMARKS:
            self.assertEqual(
                load_benchmark_source(benchmark),
                (ROOT / benchmark.source).read_text(),
            )

    def test_cli_writes_experimental_systemverilog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            direct = Path(temporary) / "ALU.sv"
            clash = Path(temporary) / "ALU.hs"
            status = main(
                [
                    str(ROOT / "examples/alu.zhl"),
                    "-o",
                    str(clash),
                    "--experimental-systemverilog",
                    str(direct),
                ]
            )
            self.assertEqual(status, 0)
            self.assertEqual(
                direct.read_text(),
                (ROOT / "examples/generated/ALU.direct.sv").read_text(),
            )

    def test_cli_stable_systemverilog_option_matches_compatibility_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stable = Path(temporary) / "stable.sv"
            compatibility = Path(temporary) / "compatibility.sv"
            source = str(ROOT / "examples/alu.zhl")
            self.assertEqual(main([source, "--systemverilog", str(stable)]), 0)
            self.assertEqual(
                main([source, "--experimental-systemverilog", str(compatibility)]), 0,
            )
            self.assertEqual(stable.read_text(), compatibility.read_text())

    @unittest.skipUnless(all(TOOLS), "Clash, Verilator, Yosys, and Icarus are required")
    def test_reproducible_experiment_runs_every_required_region(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = run_comparison(
                ROOT,
                Path(temporary) / "artifacts",
                repetitions=1,
            )

        self.assertEqual(report.schema, COMPARISON_SCHEMA)
        self.assertEqual(
            tuple(item.name for item in report.benchmarks),
            tuple(item.name for item in BENCHMARKS),
        )
        self.assertEqual(
            {item.category for item in report.benchmarks},
            {
                "datapath",
                "pipeline",
                "ready_valid",
                "credit",
                "csr",
                "request_response",
                "rules",
            },
        )
        for result in report.benchmarks:
            self.assertTrue(result.clash.behavior_passed)
            self.assertTrue(result.direct_systemverilog.behavior_passed)
            self.assertEqual(
                result.clash.synthesis.flip_flops,
                result.direct_systemverilog.synthesis.flip_flops,
            )
            self.assertEqual(
                result.clash.synthesis.logic_depth,
                result.direct_systemverilog.synthesis.logic_depth,
            )
        self.assertEqual(len(report.diagnostics), 2)
        self.assertTrue(all(item.failed_as_expected for item in report.diagnostics))
        self.assertTrue(
            all(item.generated_artifact_line for item in report.diagnostics)
        )
        self.assertTrue(
            all(not item.zlang_source_location for item in report.diagnostics)
        )
        self.assertEqual(json.loads(render_json(report))["schema"], COMPARISON_SCHEMA)


if __name__ == "__main__":
    unittest.main()
