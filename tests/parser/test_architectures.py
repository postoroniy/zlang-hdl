from pathlib import Path
import unittest

from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class ArchitectureParserTests(unittest.TestCase):
    def test_auto_architecture_has_migration_diagnostic(self) -> None:
        source = (
            "module Removed { in a:u8 out y:u8 "
            "y=architecture(auto){a} }"
        )
        with self.assertRaisesRegex(ParseError, "removed"):
            parse(source)


if __name__ == "__main__":
    unittest.main()
