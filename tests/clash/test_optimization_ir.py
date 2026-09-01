from pathlib import Path
import unittest

from zlang.backend.clash import emit
from zlang.compiler import compile_source
from zlang.opt import render
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]


class CanonicalOptimizationBackendTests(unittest.TestCase):
    def test_clash_backend_receives_losslessly_restored_ir(self) -> None:
        source = (ROOT / "examples/alu.zl").read_text()
        semantic = analyze(parse(source))
        result = compile_source(source)

        self.assertEqual(result.ir, semantic)
        self.assertEqual(result.clash, emit(semantic))

    def test_human_readable_canonical_golden_is_stable(self) -> None:
        result = compile_source((ROOT / "examples/add.zl").read_text())
        self.assertEqual(
            render(result.optimization_ir),
            (ROOT / "examples/generated/Add.opt").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
