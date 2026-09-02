from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashRuleTests(unittest.TestCase):
    def test_rule_counter_golden_matches_emitter(self) -> None:
        result = compile_source((ROOT / "examples/rule_counter.zhl").read_text())
        expected = (ROOT / "examples/generated/RuleCounter.hs").read_text()
        self.assertEqual(result.clash, expected)
        self.assertIn("rule_increment_count_fire", result.clash)
        self.assertIn("blocked0 == low", result.clash)

    def test_rule_can_atomically_drive_state_and_output_wire(self) -> None:
        result = compile_source((ROOT / "examples/rule_action.zhl").read_text())
        self.assertIn("rule___anonymous_rule_", result.clash)
        self.assertIn("fired =", result.clash)
        self.assertIn("count_next", result.clash)


if __name__ == "__main__":
    unittest.main()
