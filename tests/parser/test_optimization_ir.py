import unittest

from zlang.parser import ParseError, parse


class OptimizationIrParserBoundaryTests(unittest.TestCase):
    def test_canonicalization_requires_no_source_syntax(self) -> None:
        module = parse("module Plain { in a:u8 in b:u8 out y:u9 y=a+b }")
        self.assertEqual(module.name, "Plain")
        self.assertFalse(hasattr(module, "optimization_nodes"))

    def test_backend_internal_optimize_directive_is_not_accepted(self) -> None:
        with self.assertRaises(ParseError):
            parse("module Bad { out y:u8 optimize y y=0 }")


if __name__ == "__main__":
    unittest.main()
