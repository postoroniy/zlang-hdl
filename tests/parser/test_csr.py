from pathlib import Path
import unittest

from zlang.ast.nodes import CsrAccess
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class CsrParserTests(unittest.TestCase):
    def test_csr_addresses_fields_positions_and_access_parse(self) -> None:
        block = parse((ROOT / "examples/control_csr.zhl").read_text()).csr_blocks[0]
        self.assertEqual((block.name, block.base_address), ("control", 0x40000000))
        control = block.registers[0]
        self.assertEqual((control.name, control.offset), ("CONTROL", 0))
        self.assertEqual(control.fields[1].msb, 3)
        self.assertEqual(control.fields[1].lsb, 1)
        self.assertEqual(control.fields[2].access, CsrAccess.PULSE)


if __name__ == "__main__":
    unittest.main()
