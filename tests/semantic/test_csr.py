from pathlib import Path
import unittest

from zlang.ir.csr import CsrAccess
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class CsrSemanticTests(unittest.TestCase):
    def test_csr_map_is_canonical_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/control_csr.zl").read_text()))
        control, status = module.csr_blocks[0].registers
        self.assertEqual(control.fields[1].width, 3)
        self.assertEqual(control.fields[2].access, CsrAccess.PULSE)
        self.assertEqual(status.fields[0].reset, 1)
        self.assertEqual(status.fields[1].access, CsrAccess.WRITE_ONE_TO_CLEAR)

    def test_clock_alignment_overlap_width_and_reset_are_checked(self) -> None:
        cases = (
            (
                "module Bad { csr x @ 0 { R @ 0 { f bit rw } } }",
                "require a module clock",
            ),
            (
                "module Bad { clock c reset r csr x @ 1 { R @ 0 { f bit rw } } }",
                "base address must be 4-byte aligned",
            ),
            (
                "module Bad { clock c reset r csr x @ 0 { R @ 2 { f bit rw } } }",
                "offset must be 4-byte aligned",
            ),
            (
                "module Bad { clock c reset r csr x @ 0 { R @ 0 { a u2 @1:0 rw b bit @1 ro } } }",
                "overlaps another field",
            ),
            (
                "module Bad { clock c reset r csr x @ 0 { R @ 0 { a u3 @1:0 rw } } }",
                "range width does not match",
            ),
            (
                "module Bad { clock c reset r csr x @ 0 { R @ 0 { a u2 rw = 4 } } }",
                "does not fit u2",
            ),
        )
        for source, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                SemanticError, message
            ):
                analyze(parse(source))

    def test_all_access_policies_reach_ir(self) -> None:
        module = analyze(
            parse(
                "module Policies { clock c reset r csr p @ 0 { R @ 0 { "
                "a bit rw b bit ro c bit wo d bit w1c e bit pulse "
                "f bits<27> reserved } } }"
            )
        )
        self.assertEqual(
            [field.access.value for field in module.csr_blocks[0].registers[0].fields],
            ["rw", "ro", "wo", "w1c", "pulse", "reserved"],
        )


if __name__ == "__main__":
    unittest.main()
