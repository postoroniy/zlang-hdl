from pathlib import Path
import unittest

from zlang.ir.types import BitType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class RuleSemanticTests(unittest.TestCase):
    def test_rules_guards_actions_and_priority_reach_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/rule_counter.zhl").read_text()))
        self.assertEqual(module.rules[0].guard.type, BitType())
        self.assertEqual(module.rules[0].actions[0].target.name, "count")
        self.assertEqual(module.rule_priorities[0].higher, "clear_count")
        interface_module = analyze(
            parse((ROOT / "examples/rule_action.zhl").read_text())
        )
        self.assertEqual(interface_module.rules[0].actions[1].target.name, "fired")

    def test_ambiguous_conflicts_require_explicit_priority(self) -> None:
        source = (
            "module Bad { clock c reset r in a:bit in b:bit out y:u8 "
            "reg x:u8=0 rule one when a { x <- 1 } "
            "rule two when b { x <- 2 } y=x }"
        )
        with self.assertRaisesRegex(SemanticError, "add explicit priority"):
            analyze(parse(source))

    def test_invalid_guards_targets_priorities_and_cycles_are_rejected(self) -> None:
        cases = (
            (
                "module B { clock c reset r in a:u2 out y:u8 reg x:u8=0 rule q when a { x <- 1 } y=x }",
                "guard.*must be bit",
            ),
            (
                "module B { clock c reset r in a:bit out y:u8 reg x:u8=0 rule q when a { missing <- 1 } y=x }",
                "is not a register or output wire",
            ),
            (
                "module B { clock c reset r in a:bit out y:u8 reg x:u8=0 rule q when a { x <- 1 } priority q > missing y=x }",
                "unknown rule",
            ),
            (
                "module B { clock c reset r in a:bit out y:u8 reg x:u8=0 rule p when a { x <- 1 } rule q when a { x <- 2 } priority p > q priority q > p y=x }",
                "contains a cycle",
            ),
        )
        for source, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(SemanticError, message):
                analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
