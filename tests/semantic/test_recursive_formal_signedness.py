"""Recursive formal bindings retain canonical semantic signedness."""

from zlang.formal import build_recursive_formal_design
from zlang.parser import parse
from zlang.semantic import analyze


def test_recursive_binding_signedness_comes_from_canonical_type() -> None:
    module = analyze(parse(
        "module Typed { "
        "in signed_value:s8 in unsigned_value:u8 in raw:bits<8> in flag:bit "
        "in fixed_value:fixed<8,4> in ufixed_value:ufixed<8,4> "
        "out y:bit y=flag }"
    ))
    design = build_recursive_formal_design(module)
    signedness = {
        item.ref.local_semantic_id: item.signedness for item in design.bindings
    }
    assert signedness["port:signed_value"] == "signed"
    assert signedness["port:unsigned_value"] == "unsigned"
    assert signedness["port:raw"] == "bits"
    assert signedness["port:flag"] == "bit"
    assert signedness["port:fixed_value"] == "signed"
    assert signedness["port:ufixed_value"] == "unsigned"


def test_signed_register_observation_retains_predicate_type() -> None:
    module = analyze(parse(
        "module SignedState { clock c reset r out y:s8 "
        "reg q:s8=0 q <- q y=q }"
    ))
    design = build_recursive_formal_design(module)
    register = next(
        item for item in design.bindings
        if item.ref.local_semantic_id == "register:q"
    )
    assert register.width == 8
    assert register.signedness == "signed"
    assert any(
        observation.signedness.value == "signed"
        for concrete in design.properties
        if concrete.instance_identity == register.ref.instance_identity
        and concrete.property.predicate is not None
        for observation in concrete.property.predicate.observations()
        if observation.semantic_signal_id == register.semantic_binding_id
    )
