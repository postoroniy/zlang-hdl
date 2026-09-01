from pathlib import Path
import unittest

from zlang.ast.nodes import AddExpr, Direction, NameExpr
from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class ParserTests(unittest.TestCase):
    def test_add_example_builds_syntax_ast(self) -> None:
        module = parse((ROOT / "examples/add.zl").read_text())

        self.assertEqual(module.name, "Add")
        self.assertEqual(
            [(port.direction, port.name, port.type_name.text) for port in module.ports],
            [
                (Direction.INPUT, "a", "u8"),
                (Direction.INPUT, "b", "u8"),
                (Direction.OUTPUT, "y", "u9"),
            ],
        )
        self.assertEqual(
            module.assignments[0].expression,
            AddExpr(NameExpr("a"), NameExpr("b")),
        )

    def test_line_comments_are_ignored(self) -> None:
        module = parse("module Add { in a: u8 // input\n out y: u8 y = a }")
        self.assertEqual(module.ports[0].name, "a")

    def test_malformed_source_reports_location(self) -> None:
        with self.assertRaisesRegex(ParseError, r"line 1, column"):
            parse("module Add { in a u8 }")


if __name__ == "__main__":
    unittest.main()
