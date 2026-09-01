from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.opt.lowering import lower, restore
from zlang.semantic.errors import SemanticError
from zlang.simulate import simulate

from tests.parser.test_external_modules import SOURCE


def _compile(source: str = SOURCE):
    return compile_source(source, include_clash=False).ir


def test_external_contract_and_model_are_typed_and_simulatable() -> None:
    top = _compile()
    external = top.children[0]
    contract = external.external_contract
    assert contract is not None
    assert contract.logical_name == "VendorAdd"
    assert contract.signature == external.module_signature
    assert contract.model_callee_identity == external.functions[0].callee_identity
    assert contract.semantic_identity.startswith("extern:")
    assert simulate(external, a=255, b=1) == {"y": 256}


def test_external_contract_round_trips_through_canonical_ir() -> None:
    external = _compile().children[0]
    canonical = lower(external)
    restored = restore(canonical)
    assert restored.external_contract == external.external_contract
    assert restored.assignments == external.assignments
    with pytest.raises(ValueError, match="exactly one function"):
        replace(
            canonical,
            external_contract=replace(
                canonical.external_contract,
                model_callee_identity="missing",
            ),
        )


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        (
            "interface Bad { clock clk reset rst in a:u8 out y:u8 }",
            "cannot expose clock/reset",
        ),
        (
            "interface Bad<type T> { in a:T out y:T }",
            "non-parameterized",
        ),
        (
            "interface Bad { in a:rv<u8> out y:rv<u8> }",
            "scalar wire ports only",
        ),
        (
            "interface Bad { in a:vec<2,u8> out y:u8 }",
            "must be a scalar wire",
        ),
        (
            "interface Bad { in a:u8 out y:u8 timing { latency 1 ii 1 } }",
            "requires timing latency 0 ii 1",
        ),
    ],
)
def test_external_contract_rejects_unsupported_interface_shapes(
    fragment: str, message: str
) -> None:
    source = f"""{fragment}
fn model(a:u8)->u8 {{ a }}
extern module Ext : Bad {{ model model }}
module Top {{ in a:u8 out y:u8 inst e:Ext{{a}} y=e.y }}
"""
    with pytest.raises(SemanticError, match=message):
        _compile(source)


def test_external_model_signature_is_exact_and_non_generic() -> None:
    wrong_order = SOURCE.replace(
        "fn add_model(a : u8, b : u8) -> u9 { a + b }",
        "fn add_model(b : u8, a : u8) -> u9 { a + b }",
    )
    with pytest.raises(SemanticError, match="parameters must exactly match"):
        _compile(wrong_order)

    generic = SOURCE.replace(
        "fn add_model(a : u8, b : u8) -> u9 { a + b }",
        "fn add_model<type T>(a : T, b : T) { a + b }",
    )
    with pytest.raises(SemanticError, match="must be non-generic"):
        _compile(generic)


def test_external_model_must_exist_and_return_exact_output_type() -> None:
    with pytest.raises(SemanticError, match="does not name an existing function"):
        _compile(SOURCE.replace("model add_model", "model absent"))
    with pytest.raises(SemanticError, match="model returns u8, expected u9"):
        _compile(
            SOURCE.replace(
                "fn add_model(a : u8, b : u8) -> u9 { a + b }",
                "fn add_model(a : u8, b : u8) -> u8 { a }",
            )
        )
