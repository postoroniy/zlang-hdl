from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashSequentialTests(unittest.TestCase):
    def test_counter_golden_matches_emitter(self) -> None:
        source = (ROOT / "examples/counter.zhl").read_text()
        expected = (ROOT / "examples/generated/Counter.hs").read_text()
        self.assertEqual(compile_source(source).clash, expected)

    def test_delayed_mul_golden_matches_emitter(self) -> None:
        source = (ROOT / "examples/delayed_mul.zhl").read_text()
        expected = (ROOT / "examples/generated/DelayedMul.hs").read_text()
        self.assertEqual(compile_source(source).clash, expected)

    def test_synchronous_domain_and_explicit_clock_reset_are_emitted(self) -> None:
        generated = compile_source((ROOT / "examples/counter.zhl").read_text()).clash
        self.assertIn("vResetKind=Synchronous", generated)
        self.assertIn("Clock ZLangSystem -> Reset ZLangSystem", generated)
        self.assertIn("exposeClockResetEnable", generated)

    def test_delay_emits_requested_register_count(self) -> None:
        generated = compile_source(
            (ROOT / "examples/delayed_mul.zhl").read_text()
        ).clash
        self.assertEqual(generated.count("y_delay_s1 = register"), 1)
        self.assertEqual(generated.count("y_delay_s2 = register"), 1)


if __name__ == "__main__":
    unittest.main()
