from pathlib import Path
import unittest

from zlang.ast import nodes as ast
from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class ArchitectureParserTests(unittest.TestCase):
    def test_auto_architecture_is_not_in_the_grammar(self) -> None:
        source = (
            "module Removed { in a:u8 out y:u8 "
            "y=architecture(auto){a} }"
        )
        with self.assertRaises(ParseError):
            parse(source)

    def test_removed_scalar_forms_have_no_compatibility_ast_nodes(self) -> None:
        self.assertFalse(hasattr(ast, "ExploreExpr"))
        self.assertFalse(hasattr(ast, "ArchitectureExpr"))
        self.assertFalse(hasattr(ast, "PipelineAutoExpr"))


if __name__ == "__main__":
    unittest.main()
