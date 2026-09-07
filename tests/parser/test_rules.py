from pathlib import Path
import unittest

from zlang.ast.nodes import (
    BinaryExpr,
    ConditionalAction,
    NextAssignment,
    PriorityBlockDecl,
)
from zlang.parser import parse
from zlang.parser import ParseError
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

    def test_nested_when_else_when_is_a_recursive_action_tree(self) -> None:
        module = parse(
            "module Nested { clock clk reset rst in fault,valid,clear:bit "
            "reg state:u2=0 when fault { state <- 0 "
            "when valid { state <- 1 } "
            "else when clear { state <- 2 } "
            "else { state <- 3 } } }"
        )
        rule = module.rules[0]
        self.assertIsInstance(rule.actions[0], NextAssignment)
        branch = rule.actions[1]
        self.assertIsInstance(branch, ConditionalAction)
        assert isinstance(branch, ConditionalAction)
        self.assertEqual(branch.guard.name, "valid")
        self.assertEqual(branch.when_true[0].target, "state")
        self.assertIsNotNone(branch.when_false)
        next_branch = branch.when_false[0]
        self.assertIsInstance(next_branch, ConditionalAction)
        assert isinstance(next_branch, ConditionalAction)
        self.assertEqual(next_branch.guard.name, "clear")
        self.assertEqual(next_branch.when_true[0].expression.value, 2)
        self.assertEqual(next_branch.when_false[0].expression.value, 3)
        self.assertIsNotNone(branch.origin)
        self.assertIsNotNone(next_branch.origin)

    def test_else_binds_to_nearest_nested_when(self) -> None:
        module = parse(
            "module Nearest { clock clk reset rst in outer,a,b:bit reg x:u2=0 "
            "when outer { when a { when b { x <- 1 } else { x <- 2 } } "
            "else { x <- 3 } } }"
        )
        outer_branch = module.rules[0].actions[0]
        self.assertIsInstance(outer_branch, ConditionalAction)
        assert isinstance(outer_branch, ConditionalAction)
        inner_branch = outer_branch.when_true[0]
        self.assertIsInstance(inner_branch, ConditionalAction)
        assert isinstance(inner_branch, ConditionalAction)
        self.assertEqual(inner_branch.when_false[0].expression.value, 2)
        self.assertEqual(outer_branch.when_false[0].expression.value, 3)

    def test_uniform_action_blocks_cover_priority_arms_and_fsm_transitions(self) -> None:
        module = parse(
            "enum Phase { Idle Done } module Uniform { clock clk reset rst "
            "in go,choose:bit reg x:u1=0 "
            "priority { high: when go { when choose { x <- 1 } else {} } "
            "low: when choose { x <- 0 } } "
            "fsm phase:Phase=Idle { Idle { when go -> Done { "
            "when choose { x <- 1 } else { x <- 0 } } } Done { hold } } }"
        )
        block = next(
            item for item in module.ordered_items
            if isinstance(item, PriorityBlockDecl)
        )
        self.assertIsInstance(block.arms[0].actions[0], ConditionalAction)
        self.assertEqual(block.arms[0].actions[0].when_false, ())
        transition = module.fsms[0].states[0].transitions[0]
        self.assertIsInstance(transition.actions[0], ConditionalAction)

    def test_omitted_else_is_distinct_from_explicit_empty_else(self) -> None:
        module = parse(
            "module EmptyElse { clock clk reset rst in go,a,b:bit reg x:u1=0 "
            "when go { when a { x <- 1 } when b { x <- 0 } else {} } }"
        )
        first, second = module.rules[0].actions
        self.assertIsInstance(first, ConditionalAction)
        self.assertIsInstance(second, ConditionalAction)
        self.assertIsNone(first.when_false)
        self.assertEqual(second.when_false, ())

    def test_outer_else_chain_normalizes_to_one_conditional_rule(self) -> None:
        module = parse(
            "module RootElse { clock clk reset rst in a,b:bit reg x:u2=0 "
            "choose: when a { x <- 1 } else when b { x <- 2 } "
            "else { x <- 3 } }"
        )
        rule = module.rules[0]
        self.assertIsInstance(rule.guard, BinaryExpr)
        self.assertEqual(rule.guard.operator.value, "==")
        self.assertEqual(len(rule.actions), 1)
        root = rule.actions[0]
        self.assertIsInstance(root, ConditionalAction)
        assert isinstance(root, ConditionalAction)
        self.assertEqual(root.guard.name, "a")
        self.assertEqual(root.when_true[0].expression.value, 1)
        nested = root.when_false[0]
        self.assertIsInstance(nested, ConditionalAction)
        assert isinstance(nested, ConditionalAction)
        self.assertEqual(nested.guard.name, "b")
        self.assertEqual(nested.when_true[0].expression.value, 2)
        self.assertEqual(nested.when_false[0].expression.value, 3)

    def test_outer_else_chain_is_uniform_across_rule_forms(self) -> None:
        module = parse(
            "module RootForms { clock clk reset rst in a,b:bit reg x:u2=0 "
            "rule verbose when a { x <- 1 } else { x <- 0 } "
            "when a { x <- 1 } else when b { x <- 2 } else {} "
            "priority { high: when a { x <- 1 } else { x <- 0 } "
            "when b { x <- 2 } else {} } }"
        )
        self.assertEqual(len(module.rules), 2)
        self.assertTrue(all(len(rule.actions) == 1 for rule in module.rules))
        self.assertTrue(
            all(isinstance(rule.actions[0], ConditionalAction) for rule in module.rules)
        )
        block = next(
            item for item in module.ordered_items
            if isinstance(item, PriorityBlockDecl)
        )
        self.assertTrue(
            all(isinstance(arm.actions[0], ConditionalAction) for arm in block.arms)
        )

    def test_compile_time_if_is_not_a_runtime_action_statement(self) -> None:
        with self.assertRaises(ParseError):
            parse(
                "module SeparateIf<N=1> { clock clk reset rst in go:bit "
                "reg x:u1=0 when go { if N == 1 { x <- 1 } } }"
            )


if __name__ == "__main__":
    unittest.main()
