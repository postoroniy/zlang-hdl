from pathlib import Path
import tempfile
import unittest

from zlang.cli import main
from zlang.compiler import compile_source
from zlang.opt import render
from zlang.simulate import simulate


ROOT = Path(__file__).resolve().parents[2]


class ArchitectureIntegrationTests(unittest.TestCase):
    def test_report_canonical_ir_and_behavior_match_goldens(self) -> None:
        result = compile_source(
            (ROOT / "examples/fir_architecture.zl").read_text()
        )

        self.assertEqual(
            result.architecture_report,
            (ROOT / "examples/generated/FirArchitecture.architecture").read_text(),
        )
        self.assertEqual(
            render(result.optimization_ir),
            (ROOT / "examples/generated/FirArchitecture.opt").read_text(),
        )
        self.assertEqual(
            simulate(
                result.ir,
                samples=[1, 2, 3, 4],
                coefficients=[5, 6, 7, 8],
            )["y"],
            70,
        )

    def test_cli_writes_architecture_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "FirArchitecture.architecture"
            clash = Path(temporary) / "FirArchitecture.hs"
            status = main(
                [
                    str(ROOT / "examples/fir_architecture.zl"),
                    "-o",
                    str(clash),
                    "--architecture-report",
                    str(report),
                ]
            )
            self.assertEqual(status, 0)
            self.assertEqual(
                report.read_text(),
                (
                    ROOT / "examples/generated/FirArchitecture.architecture"
                ).read_text(),
            )


if __name__ == "__main__":
    unittest.main()
