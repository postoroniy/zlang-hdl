from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from zlang.cli import main
from zlang.compiler import compile_source
from zlang.opt import saturate
from zlang.simulate import simulate


ROOT = Path(__file__).resolve().parents[2]


class EqualitySaturationIntegrationTests(unittest.TestCase):
    def test_frozen_m26_excludes_strength_reduction(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/shift_multiply.zhl").read_text()
        )
        root = compilation.optimization_ir.assignments[0].expression
        result = saturate(compilation.optimization_ir, root)
        self.assertEqual(result.alternatives, ())
        for x in range(256):
            with self.subTest(x=x):
                self.assertEqual(simulate(compilation.ir, x=x), {"y": x * 8})

    def test_cli_writes_the_bounded_saturation_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "ShiftMultiply.saturation"
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        str(ROOT / "examples/shift_multiply.zhl"),
                        "--saturation-report",
                        str(report),
                        "--saturate-output",
                        "y",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                report.read_text(),
                (ROOT / "examples/generated/ShiftMultiply.saturation").read_text(),
            )

    def test_cli_reports_missing_or_ineligible_output(self) -> None:
        for arguments, message in (
            (["--saturation-report", "/tmp/report"], "requires --saturate-output"),
            (
                [
                    "--saturation-report",
                    "/tmp/report",
                    "--saturate-output",
                    "missing",
                ],
                "must name one assigned wire output",
            ),
        ):
            with self.subTest(message=message):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
                    with self.assertRaises(SystemExit):
                        main([str(ROOT / "examples/shift_multiply.zhl"), *arguments])
                self.assertIn(message, error.getvalue())


if __name__ == "__main__":
    unittest.main()
