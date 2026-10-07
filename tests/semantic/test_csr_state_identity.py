from dataclasses import replace
from pathlib import Path

from zlang import compile_source
import pytest

from zlang.ir.csr import CsrAccess, CsrStateBindingError, validate_state_bindings
from zlang.ir.formal import generate_properties
from zlang.opt import lower, restore


ROOT = Path(__file__).resolve().parents[2]


def bank():
    return compile_source(
        (ROOT / "stdlib/bus/reg.zhl").read_text(), top="RegBusCSRBank"
    ).ir


def test_csr_field_state_bindings_are_authoritative_and_typed():
    module = bank()
    block = module.csr_blocks[0]
    bindings = block.state_bindings
    assert [item.behavior for item in bindings] == [
        CsrAccess.READ_WRITE,
        CsrAccess.WRITE_ONE_TO_CLEAR,
        CsrAccess.PULSE,
    ]
    assert [item.field_width for item in bindings] == [1, 1, 1]
    assert [item.register_width for item in bindings] == [32, 32, 32]
    assert [item.reset_value for item in bindings] == [0, 0, 0]
    assert len({item.csr_field_id for item in bindings}) == 3
    assert len({item.implementation_state_id for item in bindings}) == 3
    assert all(item.source_origin is not None for item in bindings)
    assert [register.offset for register in block.registers] == [0, 4, 8]


def test_csr_identity_and_mapping_survive_canonical_round_trip():
    module = bank()
    restored = restore(lower(module))
    assert restored.csr_access == module.csr_access
    assert restored.csr_blocks == module.csr_blocks


def test_lowering_helper_rename_does_not_change_csr_property_identity():
    module = bank()
    block = module.csr_blocks[0]
    renamed = replace(
        block,
        state_bindings=tuple(
            replace(item, implementation_state_id=f"renamed-helper-{index}")
            for index, item in enumerate(block.state_bindings)
        ),
    )
    changed = replace(module, csr_blocks=(renamed,))
    before = generate_properties(module)
    after = generate_properties(changed)
    assert [(item.id, item.generated_from, item.expression)
            for item in before.properties] == [
        (item.id, item.generated_from, item.expression)
        for item in after.properties
    ]
    assert [item.semantic_signal_id for item in before.bindings] == [
        item.semantic_signal_id for item in after.bindings
    ]


def test_regbus_target_delegates_policy_to_semantic_bank():
    target = compile_source(
        (ROOT / "stdlib/bus/reg.zhl").read_text(), top="RegBusCSRTarget"
    ).ir
    assert [item.name for item in target.registers] == [
        "response_pending", "response_addr"
    ]
    child = next(item for item in target.children if item.name == "RegBusCSRBank")
    assert child.csr_blocks[0].state_bindings
    assert not {"rw_state", "w1c_state", "pulse_state"} & {
        item.name for item in target.registers
    }


def test_missing_duplicate_and_incompatible_bindings_are_rejected():
    block = bank().csr_blocks[0]
    bad = (
        replace(block, state_bindings=block.state_bindings[:-1]),
        replace(block, state_bindings=(block.state_bindings[0],) * 3),
        replace(block, state_bindings=(
            replace(block.state_bindings[0], field_width=2),
            *block.state_bindings[1:],
        )),
    )
    for item in bad:
        with pytest.raises(CsrStateBindingError):
            validate_state_bindings(item)
