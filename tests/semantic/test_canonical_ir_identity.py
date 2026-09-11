from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from zlang.compiler import compile_file, compile_source
from zlang.ir.expressions import ImplementationKind
from zlang.opt import canonical_ir_identity
from zlang.opt.ir import ExpressionOp


ROOT = Path(__file__).resolve().parents[2]


def test_identity_ignores_source_origin_relocation_and_whitespace() -> None:
    compact = compile_source(
        "module Add { in a:u8 in b:u8 out y:u9 y=a+b }",
        source_unit="first/location.zhl",
    )
    relocated = compile_source(
        """
// The source unit, line, columns, and raw source digest all differ.
module Add {
    in a : u8
    in b : u8
    out y : u9

    y = a + b
}
""",
        source_unit="second/location.zhl",
    )

    assert compact.ir.assignments[0].expression.origin != relocated.ir.assignments[0].expression.origin
    assert compact.high_level_ir_identity == relocated.high_level_ir_identity
    assert compact.selected_ir_identity == relocated.selected_ir_identity


def test_moving_same_standalone_source_does_not_change_identity(tmp_path: Path) -> None:
    source = """
interface PassIfc { in a:u8 out y:u8 }
module Pass : PassIfc { in a:u8 out y:u8 y=a }
"""
    first_path = tmp_path / "first" / "pass.zhl"
    second_path = tmp_path / "elsewhere" / "pass.zhl"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.write_text(source)
    second_path.write_text(source)

    first = compile_file(first_path)
    second = compile_file(second_path)

    assert first.ir.module_signature is not None
    assert second.ir.module_signature is not None
    # Standalone compile_file now assigns the basename as its logical source
    # unit; checkout directories are diagnostic/physical provenance and must
    # not alter the nominal interface ABI.
    assert (
        first.ir.module_signature.declaration_identity
        == second.ir.module_signature.declaration_identity
        == "pass.zhl::interface::PassIfc"
    )
    assert first.high_level_ir_identity == second.high_level_ir_identity
    assert first.selected_ir_identity == second.selected_ir_identity


def test_semantic_expression_change_alters_both_canonical_identities() -> None:
    xor = compile_source(
        "module Logic { in a:u8 in b:u8 out y:u8 y=a ^ b }",
    )
    bit_or = compile_source(
        "module Logic { in a:u8 in b:u8 out y:u8 y=a | b }",
    )

    assert xor.high_level_ir_identity != bit_or.high_level_ir_identity
    assert xor.selected_ir_identity != bit_or.selected_ir_identity


def test_selected_extraction_is_part_of_selected_ir_identity() -> None:
    result = compile_source(
        (ROOT / "examples/cost_mac.zhl").read_text(),
    )
    selected = result.optimization_ir
    choice_index = next(
        index
        for index, node in enumerate(selected.expressions)
        if node.op is ExpressionOp.IMPLEMENTATION_CHOICE
    )
    choice = selected.expressions[choice_index]
    assert choice.attribute("selected") is ImplementationKind.DSP_MAC
    changed_attributes = tuple(
        (name, ImplementationKind.MUL_ADD if name == "selected" else value)
        for name, value in choice.attributes
    )
    changed_choice = replace(choice, attributes=changed_attributes)
    changed = replace(
        selected,
        expressions=(
            *selected.expressions[:choice_index],
            changed_choice,
            *selected.expressions[choice_index + 1 :],
        ),
    )

    assert canonical_ir_identity(selected) != canonical_ir_identity(changed)
    # CompilationResult is replaced by synthesis-feedback paths.  The public
    # identity is deliberately derived, so it follows the replacement rather
    # than retaining a stale copied string.
    replaced_result = replace(result, optimization_ir=changed)
    assert replaced_result.selected_ir_identity == canonical_ir_identity(changed)
    assert replaced_result.selected_ir_identity != result.selected_ir_identity
    assert result.high_level_ir_identity != result.selected_ir_identity
