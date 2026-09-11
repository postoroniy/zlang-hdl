"""CLI publication policy tests.

The compiler may construct all requested backend/report artifacts internally,
but only an explicit sink (or the legacy no-option invocation) controls what
is published to stdout.
"""

from contextlib import redirect_stderr, redirect_stdout
import json
import hashlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.cli import main


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")


class CliArtifactRoutingTests(unittest.TestCase):
    source = str(ROOT / "examples/alu.zhl")

    def invoke(self, arguments: list[str]) -> tuple[int, str]:
        captured = io.StringIO()
        with redirect_stdout(captured):
            status = main([self.source, *arguments])
        return status, captured.getvalue()

    def invoke_with_stderr(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main([self.source, *arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_no_option_emits_production_systemverilog_stdout(self) -> None:
        status, stdout = self.invoke([])
        self.assertEqual(status, 0)
        self.assertIn("module ALU", stdout)
        self.assertNotIn("module ALU where", stdout)

    def test_systemverilog_only_suppresses_clash_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ALU.sv"
            status, stdout = self.invoke(["--systemverilog", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(stdout, "")
            self.assertEqual(
                output.read_text(),
                (ROOT / "examples/generated/ALU.direct.sv").read_text(),
            )

    def test_explicit_backend_can_publish_structured_source_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ALU.sv"
            source_map = Path(temporary) / "ALU.sv.zmap.json"
            status, stdout = self.invoke(
                [
                    "--systemverilog", str(output),
                    "--source-map", str(source_map),
                ]
            )
            self.assertEqual(status, 0)
            self.assertEqual(stdout, "")
            payload = json.loads(source_map.read_text())
            self.assertEqual(payload["backend"], "direct_systemverilog")
            self.assertEqual(
                payload["artifact_hash"],
                hashlib.sha256(output.read_text().encode()).hexdigest(),
            )
            self.assertTrue(payload["entries"])
            self.assertEqual(
                payload["entries"][0]["source_origin"]["source_unit"],
                Path(self.source).name,
            )
            self.assertEqual(
                payload["entries"][0]["source_origin"]["digest"],
                hashlib.sha256(Path(self.source).read_bytes()).hexdigest(),
            )

    def test_source_map_requires_one_explicit_backend_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_map = Path(temporary) / "ALU.zmap.json"
            with self.assertRaises(SystemExit) as raised:
                self.invoke(["--source-map", str(source_map)])
            self.assertEqual(raised.exception.code, 2)
            self.assertFalse(source_map.exists())

    def test_direct_only_top_is_not_blocked_by_unrelated_clash_emission(self) -> None:
        source = ROOT / "examples/multichannel_dma.zhl"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "DMAChannel.sv"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(
                    [
                        str(source), "--top", "DMAChannel",
                        "--systemverilog", str(output),
                    ]
                )
            self.assertEqual(status, 0, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("module DMAChannel", output.read_text())

    def test_check_reports_success_without_emitting_backend_text(self) -> None:
        status, stdout, stderr = self.invoke_with_stderr(["--check"])
        self.assertEqual(status, 0)
        self.assertIn("zlang: ok:", stdout)
        self.assertIn("syntax and semantics valid", stdout)
        self.assertIn("1 module", stdout)
        self.assertEqual(stderr, "")

    def test_check_validates_modules_not_selected_by_default(self) -> None:
        source = (
            "module Broken { in a:u8 out y:u8 y = missing } "
            "module DefaultTop { in a:u8 out y:u8 y = a }"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "multi.zhl"
            path.write_text(source)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main([str(path), "--check"])
            self.assertEqual(status, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("while checking module 'Broken'", stderr.getvalue())
            self.assertIn("unknown input 'missing'", stderr.getvalue())

    def test_check_with_top_checks_only_the_selected_module(self) -> None:
        source = (
            "module Broken { in a:u8 out y:u8 y = missing } "
            "module DefaultTop { in a:u8 out y:u8 y = a }"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "multi.zhl"
            path.write_text(source)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = main([str(path), "--check", "--top", "DefaultTop"])
            self.assertEqual(status, 0)
            self.assertIn("top DefaultTop", stdout.getvalue())

    def test_verbose_systemverilog_reports_written_path_without_clash_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ALU.sv"
            status, stdout, stderr = self.invoke_with_stderr(
                ["--systemverilog", str(output), "--verbose"]
            )
            self.assertEqual(status, 0)
            self.assertEqual(stdout, "")
            self.assertIn("zlang: ok:", stderr)
            self.assertIn("top ALU", stderr)
            self.assertIn(str(output), stderr)
            self.assertTrue(output.is_file())

    def test_check_rejects_artifact_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ALU.sv"
            with self.assertRaises(SystemExit) as raised:
                self.invoke(["--check", "--systemverilog", str(output)])
            self.assertEqual(raised.exception.code, 2)
            self.assertFalse(output.exists())

    def test_compatibility_systemverilog_alias_has_same_silent_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ALU-compat.sv"
            status, stdout = self.invoke(
                ["--experimental-systemverilog", str(output)]
            )
            self.assertEqual(status, 0)
            self.assertEqual(stdout, "")
            self.assertEqual(
                output.read_text(),
                (ROOT / "examples/generated/ALU.direct.sv").read_text(),
            )

    def test_clash_output_only_suppresses_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ALU.hs"
            status, stdout = self.invoke(["-o", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(stdout, "")
            self.assertIn("module ALU where", output.read_text())

    def test_combined_clash_and_systemverilog_outputs_are_both_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clash = root / "ALU.hs"
            direct = root / "ALU.sv"
            status, stdout = self.invoke(
                ["-o", str(clash), "--systemverilog", str(direct)]
            )
            self.assertEqual(status, 0)
            self.assertEqual(stdout, "")
            self.assertIn("module ALU where", clash.read_text())
            self.assertEqual(
                direct.read_text(),
                (ROOT / "examples/generated/ALU.direct.sv").read_text(),
            )

    @unittest.skipUnless(CLASH_EXECUTABLE and VERILATOR,
                         "Clash and Verilator are required")
    def test_verilog_directory_is_a_silent_explicit_sink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "rtl"
            status, stdout = self.invoke(["--verilog-dir", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(stdout, "")
            self.assertTrue((output / "ALU.sv").is_file())
            self.assertTrue(
                (output / "ALU.topEntity" / "zlang_core_ALU.v").is_file()
            )

    def test_report_only_output_is_a_silent_explicit_sink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ALU.ir"
            status, stdout = self.invoke(["--high-level-ir", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(stdout, "")
            self.assertTrue(output.is_file())
            self.assertIn("stage high_level", output.read_text())


if __name__ == "__main__":
    unittest.main()
