from pathlib import Path
import shutil
import tempfile
import unittest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.toolchain import generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]


class ExplorationM34IntegrationTests(unittest.TestCase):
    def test_combined_example_is_bounded_and_emits_clash(self):
        result = compile_source(
            (ROOT / "examples/implementation_intent.zhl").read_text(),
            top="ExploreCombined",
        )
        exploration = result.exploration_results[0]
        self.assertTrue(exploration.search_complete)
        self.assertEqual(exploration.site_kind, "implement")
        self.assertEqual(
            exploration.termination_reason,
            "all enabled bounded stages completed",
        )
        self.assertIn("best candidate in explored bounded search", result.exploration_report)
        self.assertIn("module ExploreCombined", result.clash)

    @unittest.skipUnless(
        CLASH_EXECUTABLE and shutil.which("verilator"),
        "Clash and Verilator are required",
    )
    def test_combined_example_generates_and_lints_rtl(self):
        result = compile_source(
            (ROOT / "examples/implementation_intent.zhl").read_text(),
            top="ExploreCombined",
        )
        with tempfile.TemporaryDirectory() as temporary:
            rtl = generate_verilog(
                result.clash,
                result.ir.name,
                Path(temporary),
                CLASH_EXECUTABLE,
            )
            lint_with_verilator(rtl, result.ir.name)


if __name__ == "__main__":
    unittest.main()
