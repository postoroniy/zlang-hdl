from pathlib import Path
import unittest

from zlang.ast.nodes import CsrBindingKind, CsrPriority
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class HardwareCsrParserTests(unittest.TestCase):
    def test_status_sticky_command_and_priority_parse(self) -> None:
        module = parse((ROOT / "examples/engine_csr.zhl").read_text())
        control, status = module.csr_blocks[0].registers
        self.assertEqual(control.fields[0].binding.kind, CsrBindingKind.COMMAND)
        self.assertEqual(status.fields[0].binding.kind, CsrBindingKind.STATUS)
        self.assertEqual(status.fields[1].binding.kind, CsrBindingKind.STICKY)
        self.assertEqual(status.fields[1].binding.priority, CsrPriority.HARDWARE)


if __name__ == "__main__":
    unittest.main()
