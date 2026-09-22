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

    def test_csr_base_retains_a_compile_time_parameter_expression(self) -> None:
        module = parse(
            "module Bank<BASE=0> { clock clk reset rst "
            "csr registers @ (BASE + 16) { R @0 { value bit rw = 0 } } }"
        )
        self.assertEqual(module.csr_blocks[0].base_address, "(BASE+16)")

    def test_csr_events_groups_and_split_values_parse(self) -> None:
        module = parse(
            "module Bank { clock clk reset rst out clear:bits<2> "
            "csr group Window { R @0 { value u32 rw=0 } } "
            "csr registers @0 { "
            "FAULT @0 { value u32 @31:0 ro "
            "clear_event bits<2> @1:0 on_write -> clear } "
            "windows:Window[4] @0x20 stride 4 "
            "BASE @0x40 split<32> value u64 rw=0 order low_first } }"
        )
        self.assertEqual(module.csr_groups[0].name, "Window")
        block = module.csr_blocks[0]
        self.assertEqual(block.registers[0].events[0].signal, "clear")
        self.assertEqual(block.group_uses[0].count, 4)
        self.assertEqual(block.split_registers[0].field_name, "value")


if __name__ == "__main__":
    unittest.main()
