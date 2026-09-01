from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.ir.expressions import Constant, RomRef
from zlang.ir.module import Module
from zlang.ir.storage import Rom, RomSignal
from zlang.ir.types import UIntType
from zlang.opt.ir import ExpressionOp, OptimizationStage
from zlang.opt.lowering import lower, restore
from zlang.opt.render import render
from zlang.source import SourceOrigin, SourceSpan


def _rom() -> Rom:
    origin = SourceOrigin(
        SourceSpan(3, 3, 6, 4),
        "rom:table",
        "fixture.zl",
        "a" * 64,
    )
    return Rom(
        name="table",
        semantic_id="rom:R:table",
        element_type=UIntType(8),
        depth=3,
        address_type=UIntType(2),
        read_latency=1,
        contents=tuple(Constant(value, UIntType(8), origin=origin) for value in (1, 7, 9)),
        read_address=Constant(0, UIntType(2), origin=origin),
        initialization_identity="init:fixture:values",
        dependency_identity=(("fixture", "b" * 64),),
        evaluator_schema="zlang-compile-time-v1",
        content_hash="c" * 64,
        source_origin=origin,
    )


def test_rom_and_reference_lower_and_restore_losslessly() -> None:
    rom = _rom()
    module = Module(
        "R",
        (),
        (),
        clock="clk",
        reset="rst",
        roms=(rom,),
    )

    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    assert canonical.roms[0].semantic_id == rom.semantic_id
    assert canonical.roms[0].contents
    assert canonical.roms[0].content_hash == rom.content_hash
    assert restore(canonical) == module

    text = render(canonical)
    assert "state.rom name=table" in text
    assert "semantic_id=rom:R:table" in text
    assert f"content_hash={'c' * 64}" in text


def test_rom_reference_has_one_cycle_state_metadata_and_round_trips() -> None:
    rom = _rom()
    # Put the reference in an initializer solely to exercise the expression DAG;
    # semantic analysis owns the legality of where ROM data may be consumed.
    module = Module(
        "R",
        (),
        (),
        clock="clk",
        reset="rst",
        roms=(rom,),
        functions=(),
        locals=(),
    )
    reference_module = replace(
        module,
        roms=(replace(rom, contents=(RomRef("table", RomSignal.READ_DATA, UIntType(8)),) * 3),),
    )
    canonical = lower(reference_module)
    reference = next(node for node in canonical.expressions if node.op is ExpressionOp.ROM_REF)
    assert reference.metadata.latency == 1
    assert "read_state" in {effect.value for effect in reference.metadata.effects}
    assert restore(canonical) == reference_module


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"semantic_id": ""}, "semantic identity"),
        ({"depth": 0}, "depth"),
        ({"read_latency": 2}, "read latency"),
        ({"address_type": UIntType(3)}, "address type"),
        ({"content_hash": ""}, "content hash"),
    ),
)
def test_rom_ir_rejects_malformed_identity_and_shape(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_rom(), **change)
