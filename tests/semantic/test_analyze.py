from pathlib import Path
import unittest

from zlang.ir.expressions import Add, InputRef
from zlang.ir.types import UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class SemanticTests(unittest.TestCase):
    def test_add_example_produces_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/add.zhl").read_text()))

        expression = module.assignments[0].expression
        self.assertEqual(expression.type, UIntType(9))
        self.assertEqual(
            expression,
            Add(InputRef("a", UIntType(8)), InputRef("b", UIntType(8)), UIntType(9)),
        )

    def test_narrow_output_rejects_carry_loss(self) -> None:
        source = "module Bad { in a: u8 in b: u8 out y: u8 y = a + b }"
        with self.assertRaisesRegex(
            SemanticError, "cannot assign u9 expression to u8 output 'y'"
        ):
            analyze(parse(source))

    def test_unknown_input_is_rejected(self) -> None:
        source = "module Bad { in a: u8 out y: u9 y = a + missing }"
        with self.assertRaisesRegex(SemanticError, "unknown input 'missing'"):
            analyze(parse(source))

    def test_duplicate_port_is_rejected(self) -> None:
        source = "module Bad { in a: u8 in a: u8 out y: u8 y = a }"
        with self.assertRaisesRegex(SemanticError, "duplicate port 'a'"):
            analyze(parse(source))

    def test_input_assignment_is_rejected(self) -> None:
        source = "module Bad { in a: u8 out y: u8 a = a y = a }"
        with self.assertRaisesRegex(SemanticError, "cannot assign to input 'a'"):
            analyze(parse(source))

    def test_unassigned_output_is_rejected(self) -> None:
        source = "module Bad { in a: u8 out y: u8 }"
        with self.assertRaisesRegex(SemanticError, "output 'y' has no assignment"):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
