from pathlib import Path
import unittest

from zlang.ir.types import BitType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.semantic.analyze import _has_priority_cycle, _priority_orders


ROOT = Path(__file__).resolve().parents[2]


class RuleSemanticTests(unittest.TestCase):
    def test_priority_helpers_match_exhaustive_three_node_graph_oracle(self) -> None:
        nodes = ("a", "b", "c")
        pairs = tuple((first, second) for first in nodes for second in nodes)
        for mask in range(1 << len(pairs)):
            edges = {edge for index, edge in enumerate(pairs) if mask & (1 << index)}
            # Independent transitive closure starts with actual edges, not
            # reflexive paths: a true diagonal therefore means a real cycle.
            closure = {pair: pair in edges for pair in pairs}
            for intermediate in nodes:
                for first, second in pairs:
                    closure[first, second] |= (
                        closure[first, intermediate] and closure[intermediate, second]
                    )
            with self.subTest(graph=mask):
                for first, second in pairs:
                    self.assertEqual(
                        _priority_orders(first, second, edges),
                        first == second or closure[first, second] or closure[second, first],
                    )
                expected_cycle = any(closure[node, node] for node in nodes)
                # The retained helper signature does not restrict traversal to
                # `names`; the semantic caller separately validates all edges.
                for names in (set(nodes), {"a"}, set()):
                    self.assertEqual(_has_priority_cycle(names, edges), expected_cycle)

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
