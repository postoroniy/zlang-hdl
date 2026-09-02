from pathlib import Path
import unittest

from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]


class RuleParserTests(unittest.TestCase):
    def test_guard_actions_and_priority_parse(self) -> None:
        module = parse((ROOT / "examples/rule_counter.zhl").read_text())
        block = next(item for item in module.ordered_items if item.__class__.__name__ == "PriorityBlockDecl")
        self.assertEqual([arm.label for arm in block.arms], ["clear_count", "increment_count"])
        self.assertEqual(block.arms[0].actions[0].target, "count")
        typed = analyze(module)
        self.assertEqual([rule.name for rule in typed.rules], ["clear_count", "increment_count"])
        self.assertEqual(
            (typed.rule_priorities[0].higher, typed.rule_priorities[0].lower),
            ("clear_count", "increment_count"),
        )


if __name__ == "__main__":
    unittest.main()
