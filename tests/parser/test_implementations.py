from pathlib import Path
import unittest

from zlang.ast.nodes import ImplementationChoiceExpr, ImplementationKind
from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class ImplementationChoiceParserTests(unittest.TestCase):
    def test_selected_kind_and_alternatives_are_explicit_in_syntax_ast(self) -> None:
        module = parse((ROOT / "examples/mac_choice.zhl").read_text())
        choice = module.assignments[0].expression

        self.assertIsInstance(choice, ImplementationChoiceExpr)
        self.assertEqual(choice.selected, ImplementationKind.DSP_MAC)
        self.assertEqual(
            [alternative.kind for alternative in choice.alternatives],
            [ImplementationKind.MUL_ADD, ImplementationKind.DSP_MAC],
        )

    def test_choice_requires_two_arms_and_known_implementation_kinds(self) -> None:
        invalid = (
            "module One { in a:u8 in b:u8 in c:u16 out y:u17 "
            "y=choice(mul_add){mul_add=>a*b+c} }",
            "module Unknown { in a:u8 in b:u8 in c:u16 out y:u17 "
            "y=choice(custom){mul_add=>a*b+c dsp_mac=>a*b+c} }",
        )
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaises(ParseError):
                    parse(source)


if __name__ == "__main__":
    unittest.main()
