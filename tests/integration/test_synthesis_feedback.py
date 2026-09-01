from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.cli import main
from zlang.compiler import compile_source
from zlang.synthesis import characterize_with_yosys, render_synthesis_report


ROOT = Path(__file__).resolve().parents[2]
YOSYS = shutil.which("yosys")


class SynthesisFeedbackIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(CLASH_EXECUTABLE and YOSYS, "Clash and Yosys are required")
    def test_identical_candidates_are_loaded_from_stable_cache(self) -> None:
        compilation = compile_source((ROOT / "examples/cost_mac.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            first = characterize_with_yosys(
                compilation.ir,
                cache,
                clash_executable=CLASH_EXECUTABLE,
                yosys_executable=YOSYS,
            )
            cache_times = {
                path.name: path.stat().st_mtime_ns for path in cache.glob("*.json")
            }
            second = characterize_with_yosys(
                compilation.ir,
                cache,
                clash_executable=CLASH_EXECUTABLE,
                yosys_executable=YOSYS,
            )

            self.assertEqual(len(cache_times), 2)
            self.assertTrue(all(not item.cache_hit for item in first.candidates))
            self.assertTrue(all(item.cache_hit for item in second.candidates))
            self.assertEqual(
                cache_times,
                {
                    path.name: path.stat().st_mtime_ns
                    for path in cache.glob("*.json")
                },
            )
            self.assertEqual(first.module, second.module)
            report = render_synthesis_report(second)
            self.assertIn("Yosys", report)
            self.assertIn("target=generic-lut6", report)
            self.assertIn("cache_hit=true", report)
            self.assertIn("estimate lut=", report)
            self.assertIn("measured target=", report)

    @unittest.skipUnless(CLASH_EXECUTABLE, "Clash is required for tool diagnostics")
    def test_missing_yosys_has_an_actionable_cli_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            error = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(error):
                with self.assertRaises(SystemExit):
                    main(
                        [
                            str(ROOT / "examples/cost_mac.zl"),
                            "--synthesis-report",
                            str(Path(temporary) / "report.txt"),
                            "--synthesis-cache",
                            str(Path(temporary) / "cache"),
                            "--clash",
                            str(CLASH_EXECUTABLE),
                            "--yosys",
                            "/definitely/missing/yosys",
                        ]
                    )
            self.assertIn("Yosys executable was not found", error.getvalue())


if __name__ == "__main__":
    unittest.main()
