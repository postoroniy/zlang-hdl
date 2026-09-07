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

    def test_internal_output_read_recommends_an_immutable_local(self) -> None:
        source = """module Bad {
            clock clk reset rst
            in ready:bit out valid:bit
            reg pending:bit=0
            valid=pending
            when valid & ready { pending <- 0 }
        }"""
        with self.assertRaises(SemanticError) as caught:
            analyze(parse(source))

        error = caught.exception
        self.assertEqual(error.code, "ZL-SEMANTIC-OUTPUT-READ")
        self.assertEqual(
            str(error),
            "module output 'valid' cannot be read internally; drive it from "
            "an immutable local and read that local instead",
        )
        self.assertEqual(error.primary.construct, "name valid")
        self.assertEqual(
            error.fixes,
            (
                "bind the driving expression to an immutable local and use "
                "that local both internally and for the output assignment",
            ),
        )

        remedy = source.replace(
            "valid=pending\n            when valid & ready",
            "driven_valid=pending\n            valid=driven_valid\n            "
            "when driven_valid & ready",
        )
        module = analyze(parse(remedy))
        self.assertIn("driven_valid", {item.name for item in module.locals})

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
