from pathlib import Path
import unittest

from zlang.ast.nodes import DelayExpr, NextAssignment, RegisterDecl
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class SequentialParserTests(unittest.TestCase):
    def test_counter_state_syntax_parses(self) -> None:
        module = parse((ROOT / "examples/counter.zl").read_text())
        self.assertEqual(module.clocks, ("clk",))
        self.assertEqual(module.resets, ("rst",))
        self.assertIsInstance(module.registers[0], RegisterDecl)
        self.assertEqual(module.registers[0].name, "count")
        self.assertIsInstance(module.next_assignments[0], NextAssignment)

    def test_delay_syntax_parses(self) -> None:
        module = parse((ROOT / "examples/delayed_mul.zl").read_text())
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, DelayExpr)
        self.assertEqual(expression.cycles, 2)


if __name__ == "__main__":
    unittest.main()
