from pathlib import Path
import unittest

from zlang.backend.clash import emit
from zlang.compiler import compile_source
from zlang.implementations import (
    ImplementationSelectionError,
    select_implementation,
)


ROOT = Path(__file__).resolve().parents[2]


class ImplementationChoiceClashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compilation = compile_source(
            (ROOT / "examples/mac_choice.zhl").read_text()
        )

    def test_selected_dsp_mac_and_report_match_goldens(self) -> None:
        self.assertEqual(
            self.compilation.clash,
            (ROOT / "examples/generated/MacChoice.hs").read_text(),
        )
        self.assertIn("let zlangDspMac", self.compilation.clash)
        self.assertEqual(
            self.compilation.implementation_report,
            (ROOT / "examples/generated/MacChoice.implementations").read_text(),
        )

    def test_explicit_mul_add_selection_uses_its_own_typed_pipeline(self) -> None:
        module = select_implementation(self.compilation.ir, "y", "mul_add")
        generated = emit(module)

        self.assertEqual(
            generated,
            (ROOT / "examples/generated/MacChoiceMulAdd.hs").read_text(),
        )
        self.assertNotIn("zlangDspMac", generated)

    def test_selection_diagnostics_do_not_fall_back_silently(self) -> None:
        with self.assertRaisesRegex(ImplementationSelectionError, "unknown"):
            select_implementation(self.compilation.ir, "y", "unknown")
        with self.assertRaisesRegex(ImplementationSelectionError, "does not have"):
            select_implementation(self.compilation.ir, "missing", "mul_add")


if __name__ == "__main__":
    unittest.main()
