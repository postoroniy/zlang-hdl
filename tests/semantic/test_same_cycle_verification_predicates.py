from __future__ import annotations

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir.formal_predicates import FormalPredicate
from zlang.opt import lower, restore
from zlang.semantic import SemanticError


HARDWARE = """
struct Pair { high : u4 low : u4 }

fn packed_value(high : u4, low : u4) -> bits<8> {
    concat(high, low)
}

module SameCyclePredicateSurface {
    clock clk reset rst
    in high : u4
    in low : u4
    in signed_part : s4
    in choose : bit
    in selector : u1
    out joined : bits<8>
    out signed_joined : bits<8>
    out selected : u4

    pair : Pair = Pair { high low }
    tuple_value : (u4, u4) = (high, low)
    joined = packed_value(high, low)
    signed_joined = concat(signed_part, low)
    selected = switch selector { 0 => high else => low }
}
"""


VERIFIED = HARDWARE.rsplit("}", 1)[0] + """
    assert packed_matches {
        joined == packed_value(pair.high, tuple_value[1])
    }
    assert mux_matches {
        mux(choose, high, low) == mux(choose, pair.high, tuple_value[1])
    }
    assert switch_matches {
        selected == switch selector { 0 => pair.high else => tuple_value[1] }
    }
    assert signed_representation {
        signed_joined == concat(signed_part, low)
    }
    assert exact_bitcast {
        bitcast<u8>(joined) == bitcast<u8>(concat(pair.high, tuple_value[1]))
    }
    assert arithmetic_commutes { (high + low) == (low + high) }
    assert bit_logic_identity {
        ((high & low) ^ (high | low)) == (high ^ low)
    }
    cover high_nibble_seen { joined[7:4] == 15 }
}
"""


def _source_properties(compilation):
    return tuple(
        item
        for item in compilation.formal_design.properties
        if (item.generated_from or "").startswith("verification-")
    )


def test_complete_pure_same_cycle_surface_uses_structured_predicates() -> None:
    compilation = compile_source(
        VERIFIED,
        include_clash=False,
        source_unit="tests/fixtures/same_cycle_predicates.zl",
    )
    properties = _source_properties(compilation)
    assert tuple(item.generated_from for item in properties) == (
        "verification-assert:$module:packed_matches",
        "verification-assert:$module:mux_matches",
        "verification-assert:$module:switch_matches",
        "verification-assert:$module:signed_representation",
        "verification-assert:$module:exact_bitcast",
        "verification-assert:$module:arithmetic_commutes",
        "verification-assert:$module:bit_logic_identity",
    )
    assert all(item.predicate is not None for item in properties)
    assert all(item.expression == item.predicate.render() for item in properties)

    packed = properties[0]
    assert packed.predicate is not None
    assert packed.predicate.observation_ids() == (
        "port:joined",
        "port:high",
        "port:low",
    )
    assert "<< 4" in packed.predicate.render()
    assert "| resize<8,bits>(port:low)" in packed.predicate.render()

    signed = properties[3]
    assert signed.predicate is not None
    assert "resize<8,bits>(port:signed_part)" in signed.predicate.render()
    assert "resize<8,signed>(port:signed_part)" not in signed.predicate.render()

    for item in (*properties, *compilation.formal_design.covers):
        assert item.predicate is not None
        assert FormalPredicate.from_data(item.predicate.to_data()) == item.predicate
        assert item.source_origin is not None
        assert item.source_origin.source_unit == (
            "tests/fixtures/same_cycle_predicates.zl"
        )
        assert item.source_origin.construct

    canonical = lower(compilation.ir)
    assert restore(canonical) == compilation.ir


def test_verification_surface_cannot_change_production_rtl_or_artifact_hashes() -> None:
    plain = compile_source(HARDWARE, include_clash=False)
    verified = compile_source(VERIFIED, include_clash=False)
    assert verified.high_level_ir_identity == plain.high_level_ir_identity
    assert verified.selected_ir_identity == plain.selected_ir_identity

    plain_sv = emit_sv_artifact(
        plain.ir, selected_ir_identity=plain.selected_ir_identity
    )
    verified_sv = emit_sv_artifact(
        verified.ir, selected_ir_identity=verified.selected_ir_identity
    )
    assert verified_sv.text == plain_sv.text
    assert verified_sv.artifact_hash == plain_sv.artifact_hash

    plain_clash = emit_clash_artifact(
        plain.ir, selected_ir_identity=plain.selected_ir_identity
    )
    verified_clash = emit_clash_artifact(
        verified.ir, selected_ir_identity=verified.selected_ir_identity
    )
    assert verified_clash.text == plain_clash.text
    assert verified_clash.artifact_hash == plain_clash.artifact_hash


@pytest.mark.parametrize(
    "source",
    (
        "module Temporal { clock c reset r in a:bit out y:bit y=a "
        "assert unsupported { delay<1>(a) } }",
        "module Functional { clock c reset r in values:vec<2,bit> out y:bit "
        "y=values[0] assert unsupported { reduce(|, values) } }",
        "module Child { in a:bit out y:bit y=a } "
        "module Hierarchical { clock c reset r in a:bit out y:bit "
        "child:Child child.a=a y=child.y assert unsupported { child.y } }",
    ),
)
def test_temporal_functional_and_hidden_child_predicates_fail_closed(
    source: str,
) -> None:
    with pytest.raises(SemanticError) as caught:
        compile_source(source, include_clash=False)
    assert caught.value.code == "ZL-VERIFY-PREDICATE"
    assert caught.value.primary is not None
    assert "verification clause 'unsupported'" in str(caught.value)


def test_range_proven_runtime_index_lowers_to_existing_mux_predicate() -> None:
    source = """
    module RuntimePredicate {
        clock clk reset rst
        in raw : bits<4>
        in index : u2
        out y : bit
        values : vec<4,bit> = bitcast<vec<4,bit>>(raw)
        y = values[index]
        assert selected_bit { y == values[index] }
    }
    """
    compilation = compile_source(source, include_clash=False)
    property_ = next(
        item for item in compilation.formal_design.properties
        if item.generated_from == "verification-assert:$module:selected_bit"
    )
    assert property_.predicate is not None
    assert property_.predicate.observation_ids() == (
        "port:y",
        "port:index",
        "port:raw",
    )
    rendered = property_.predicate.render()
    assert "port:index == 0" in rendered
    assert "port:index == 1" in rendered
    assert "port:index == 2" in rendered
    assert "shift_right" not in rendered
    assert ">>" in rendered
    assert FormalPredicate.from_data(
        property_.predicate.to_data()
    ) == property_.predicate
