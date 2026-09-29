from pathlib import Path
import unittest

from zlang.ir.csr import CsrAccess
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class CsrSemanticTests(unittest.TestCase):
    def test_csr_map_is_canonical_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/control_csr.zhl").read_text()))
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
        self.assertEqual(
            [binding.behavior.value for binding in module.csr_blocks[0].state_bindings],
            ["rw", "wo", "w1c", "pulse"],
        )

    def test_uncovered_register_bits_are_implicit_reserved_space(self) -> None:
        module = analyze(parse(
            "module Gaps { clock c reset r csr p @0 { "
            "R @0 { low bit @0 rw=0 high bit @31 ro } } }"
        ))
        register = module.csr_blocks[0].registers[0]
        self.assertEqual(
            [(field.name, field.msb, field.lsb) for field in register.fields],
            [("low", 0, 0), ("high", 31, 31)],
        )

    def test_csr_base_is_resolved_from_the_specialized_value_environment(self) -> None:
        module = analyze(
            parse(
                "module Bank<BASE=0> { clock clk reset rst "
                "csr registers @ (BASE + 16) { R @0 { value bit rw = 0 } } }"
            )
        )
        self.assertEqual(module.csr_blocks[0].base_address, 16)

    def test_invalid_compile_time_csr_bases_fail_semantically(self) -> None:
        cases = (
            ("<BASE>", "BASE", "unresolved compile-time parameter 'BASE'"),
            ("<BASE=0>", "(BASE + 1)", "base address must be 4-byte aligned"),
        )
        for parameters, base, message in cases:
            source = (
                f"module Bank{parameters} {{ clock clk reset rst "
                f"csr registers @ {base} {{ R @0 {{ value bit rw = 0 }} }} }}"
            )
            with self.subTest(base=base), self.assertRaisesRegex(
                SemanticError, message
            ):
                analyze(parse(source))

    def test_specialized_csr_bases_participate_in_overlap_validation(self) -> None:
        source = (
            "module Bank<BASE=0> { clock clk reset rst "
            "csr first @ BASE { A @0 { value bit rw = 0 } } "
            "csr second @ (BASE + 0) { B @0 { value bit rw = 0 } } }"
        )
        with self.assertRaisesRegex(SemanticError, "overlapping CSR address"):
            analyze(parse(source))

    def test_access_observations_are_independent_of_storage_ownership(self) -> None:
        module = analyze(parse(
            "module Bank { clock clk reset rst out clear:bits<2> "
            "csr registers @0 { FAULT @0 { value u32 @31:0 ro "
            "clear_event bits<2> @1:0 on_write -> clear } } }"
        ))
        block = module.csr_blocks[0]
        self.assertEqual(len(block.state_bindings), 0)
        self.assertEqual(len(block.access_observations), 1)
        self.assertEqual(block.registers[0].events[0].name, "clear_event")

    def test_inline_groups_expand_in_one_bank_and_split_has_one_logical_view(self) -> None:
        module = analyze(parse(
            "module Bank { clock clk reset rst "
            "csr group Window { R @0 { value u32 rw=0 } } "
            "csr registers @0 { windows:Window[3] @0x20 stride 4 "
            "BASE @0x40 split<32> value u64 rw=0x123456789abcdef0 "
            "order low_first } }"
        ))
        block = module.csr_blocks[0]
        self.assertEqual(
            [(item.name, item.offset) for item in block.registers],
            [
                ("windows_0_R", 0x20),
                ("windows_1_R", 0x24),
                ("windows_2_R", 0x28),
                ("BASE_LOW", 0x40),
                ("BASE_HIGH", 0x44),
            ],
        )
        self.assertEqual(
            [item.fields[0].reset for item in block.registers[-2:]],
            [0x9ABCDEF0, 0x12345678],
        )
        self.assertEqual(str(block.split_views[0].canonical_type), "u64")

    def test_group_split_values_share_normal_expansion_and_extent_rules(self) -> None:
        module = analyze(parse(
            "module Bank { clock clk reset rst "
            "csr group Window { CTRL @0 { enable bit @0 rw=0 } "
            "BASE @4 split<32> value u64 rw=0x123456789abcdef0 "
            "order low_first LIMIT @12 split<32> value u64 rw=0 "
            "order low_first } "
            "csr registers @0 { rows:Window[2] @0 stride 20 } }"
        ))
        block = module.csr_blocks[0]
        self.assertEqual(
            [(item.name, item.offset) for item in block.registers],
            [
                ("rows_0_CTRL", 0),
                ("rows_1_CTRL", 20),
                ("rows_0_BASE_LOW", 4),
                ("rows_0_BASE_HIGH", 8),
                ("rows_0_LIMIT_LOW", 12),
                ("rows_0_LIMIT_HIGH", 16),
                ("rows_1_BASE_LOW", 24),
                ("rows_1_BASE_HIGH", 28),
                ("rows_1_LIMIT_LOW", 32),
                ("rows_1_LIMIT_HIGH", 36),
            ],
        )
        self.assertEqual(
            [item.name for item in block.split_views],
            ["rows_0_BASE", "rows_0_LIMIT", "rows_1_BASE", "rows_1_LIMIT"],
        )
        self.assertEqual(block.split_views[0].projection_path, ("rows[0]", "BASE"))

        with self.assertRaisesRegex(SemanticError, "smaller than group extent"):
            analyze(parse(
                "module Bad { clock clk reset rst "
                "csr group G { BASE @4 split<32> value u64 rw=0 "
                "order low_first } "
                "csr registers @0 { rows:G[1] @0 stride 8 } }"
            ))

    def test_group_bounds_stride_and_overlaps_fail_closed(self) -> None:
        cases = (
            (
                "csr group G { A @0 { v u32 rw=0 } B @4 { v u32 rw=0 } } "
                "csr registers @0 { rows:G[2] @0 stride 4 }",
                "smaller than group extent",
            ),
            (
                "csr group G { A @0 { v u32 rw=0 } } "
                "csr registers @0 { rows:G[65] @0 stride 4 }",
                "count must be in 1..64",
            ),
        )
        for declarations, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                SemanticError, message
            ):
                analyze(parse(
                    f"module Bad {{ clock clk reset rst {declarations} }}"
                ))


if __name__ == "__main__":
    unittest.main()
