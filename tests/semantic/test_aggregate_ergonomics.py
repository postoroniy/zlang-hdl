from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.types import UIntType, VecType
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate


def _compile(source: str):
    return compile_source(source).ir


def test_vector_literal_repeat_and_struct_update_are_exact_existing_ir() -> None:
    module = _compile(
        "struct Beat { data:u8 last:bit } module AggregateSugar { "
        "in beat:Beat out literal:vec<3,u8> out repeated:vec<3,u8> "
        "out updated:Beat literal=[1,2,3] repeated=repeat(7) "
        "updated=beat with { last=1 } }"
    )
    values = {item.target.name: item.expression for item in module.assignments}
    assert isinstance(values["literal"], expr.Generate)
    assert isinstance(values["repeated"], expr.Generate)
    assert values["literal"].type == VecType(3, UIntType(8))
    assert isinstance(values["updated"], expr.StructConstruct)
    assert simulate(module, beat={"data": 0xA5, "last": 0}) == {
        "literal": [1, 2, 3],
        "repeated": [7, 7, 7],
        "updated": {"data": 0xA5, "last": 1},
    }
    assert restore(lower(module)).assignments == module.assignments


def test_aggregate_equality_is_recursive_and_exact() -> None:
    module = _compile(
        "struct Pair { data:vec<2,u8> flag:bit } module AggregateEq { "
        "in a:Pair in b:Pair out same:bit out different:bit "
        "same=a==b different=a!=b }"
    )
    assert simulate(
        module,
        a={"data": [1, 2], "flag": 1},
        b={"data": [1, 2], "flag": 1},
    ) == {"same": 1, "different": 0}
    assert simulate(
        module,
        a={"data": [1, 2], "flag": 1},
        b={"data": [1, 3], "flag": 1},
    ) == {"same": 0, "different": 1}


def test_exhaustive_destructure_lowers_to_ordinary_immutable_field_values() -> None:
    concise = _compile(
        "struct Beat { data:u8 last:bit } module Destructure { "
        "in beat:Beat out y:Beat Beat { data, last } = beat "
        "y=Beat { data last } }"
    )
    verbose = _compile(
        "struct Beat { data:u8 last:bit } module Destructure { "
        "in beat:Beat out y:Beat "
        "whole:Beat=beat data:u8=whole.data last:bit=whole.last "
        "y=Beat { data last } }"
    )
    assert concise.assignments == verbose.assignments
    assert restore(lower(concise)).assignments == concise.assignments
    assert simulate(concise, beat={"data": 0xA5, "last": 1}) == {
        "y": {"data": 0xA5, "last": 1}
    }


@pytest.mark.parametrize(
    ("source", "message"),
    (
        ("module M { out y:vec<2,u8> y=[1] }", "has 1 elements"),
        ("module M { out y:vec<2,u8> y=repeat<3>(1) }", "does not match"),
        (
            "struct S { a:u8 } module M { in x:S out y:S y=x with { b=1 } }",
            "has no field",
        ),
        (
            "struct S { a:u8 b:bit } module M { in x:S S { a }=x }",
            "must name every field exactly once",
        ),
        (
            "struct S { a:u8 } module M { in x:S S { a,a }=x }",
            "repeats field",
        ),
        (
            "struct S { a:u8 } module M { in a:u8 in x:S S { a }=x }",
            "shadows an existing symbol",
        ),
    ),
)
def test_aggregate_sugar_rejects_ambiguous_or_malformed_forms(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)
