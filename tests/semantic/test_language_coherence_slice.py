from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from zlang.parser import parse
from zlang.compiler import compile_source
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate
from zlang.stdlib import load_stdlib_source


def test_declarations_may_follow_modules_and_declaration_only_units_parse() -> None:
    ordered = parse(
        "module Top { in x:u8 out y:u8 y=identity(x) } "
        "fn identity(x:u8)->u8{x}"
    )
    assert ordered.name == "Top"
    assert tuple(item.name for item in ordered.functions) == ("identity",)

    declarations = parse(
        "struct Box<type T>{value:T} fn identity<type T>(x:T)->T{x}"
    )
    assert declarations.declaration_only
    assert declarations.submodules == ()
    assert tuple(item.name for item in declarations.structs) == ("Box",)
    assert tuple(item.name for item in declarations.functions) == ("identity",)


def test_declaration_only_stdlib_unit_is_not_published_as_child() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "test").mkdir()
        (root / "test" / "declarations.zhl").write_text(
            "struct Box<type T>{value:T} fn box<type T>(x:T){Box{value=x}}\n"
        )
        with patch("zlang.stdlib._ROOTS", (root,)):
            record = load_stdlib_source("std.test.declarations")
            assert record.ast.declaration_only
            module = analyze(
                parse(
                    "import std.test.declarations "
                    "module Top{in x:u8 out y:Box<u8> y=box(x)}"
                )
            )
    assert module.instances == ()
    assert all(item.name != "__declaration_unit__" for item in module.children)


def test_exact_bitwise_complement_preserves_width_and_representation() -> None:
    module = analyze(
        parse(
            "module Complement { in b:bit in raw:bits<4> in u:u4 in s:s4 "
            "out nb:bit out nraw:bits<4> out nu:u4 out ns:s4 "
            "nb=~b nraw=~raw nu=~u ns=~s }"
        )
    )
    assert simulate(module, b=0, raw=0b0101, u=0b0011, s=-3) == {
        "nb": 1,
        "nraw": 0b1010,
        "nu": 0b1100,
        "ns": 2,
    }
    assert tuple(str(item.expression.type) for item in module.assignments) == (
        "bit",
        "bits<4>",
        "u4",
        "s4",
    )


@pytest.mark.parametrize("type_name", ["fixed<8,4>", "vec<2,u4>"])
def test_bitwise_complement_rejects_non_scalar_representation_types(
    type_name: str,
) -> None:
    with pytest.raises(SemanticError, match="bitwise complement requires"):
        analyze(parse(f"module Bad{{in x:{type_name} out y:{type_name} y=~x}}"))


def test_module_where_constraint_is_discharged_at_specialization() -> None:
    source = (
        "module FifoLike<type T,D=4> "
        "where D >= 2 && is_power_of_two(D) { in x:T out y:T y=x } "
        "module Top { in x:u8 out y:u8 "
        "inst f:FifoLike<T=u8,D=4>{x} y=f.y }"
    )
    assert compile_source(source, include_clash=False).ir.name == "Top"

    with pytest.raises(SemanticError) as captured:
        compile_source(source.replace("D=4>{x}", "D=3>{x}"), include_clash=False)
    assert captured.value.code == "ZL-SEMANTIC-PARAMETER-CONSTRAINT"
    assert "not satisfied" in str(captured.value)


def test_module_where_final_uppercase_arithmetic_is_not_a_type_condition() -> None:
    source = (
        "module Shape<DEPTH=8,ROWS=2,BANKS=4> "
        "where DEPTH == ROWS * BANKS { in x:u8 out y:u8 y=x } "
        "module Top { in x:u8 out y:u8 s:Shape<DEPTH=8,ROWS=2,BANKS=4>{x} y=s.y }"
    )
    assert compile_source(source, include_clash=False).ir.name == "Top"
    with pytest.raises(SemanticError, match="constraint is not satisfied"):
        compile_source(
            source.replace("DEPTH=8,ROWS=2,BANKS=4>{x}", "DEPTH=7,ROWS=2,BANKS=4>{x}"),
            include_clash=False,
        )


def test_nominal_type_condition_composes_with_value_conjunction() -> None:
    source = (
        "struct Box<type X>{value:X} "
        "module Child<type T,N> where T == Box<u8> && N == 1 "
        "{ in x:u8 out y:u8 y=x } "
        "module Top { in x:u8 out y:u8 "
        "child:Child<T=Box<u8>,N=1>{x=x} y=child.y }"
    )
    assert compile_source(source, top="Top", include_clash=False).ir.name == "Top"
    with pytest.raises(SemanticError, match="constraint is not satisfied"):
        compile_source(
            source.replace("T=Box<u8>,N=1", "T=Box<u8>,N=2"),
            top="Top",
            include_clash=False,
        )


def test_module_where_does_not_become_runtime_hardware() -> None:
    result = compile_source(
        "module Child<N=8> where N == 8 { in x:u8 out y:u8 y=x } "
        "module Top{in x:u8 out y:u8 inst c:Child<8>{x} y=c.y}",
        include_clash=False,
    )
    child = result.ir.children[0]
    assert not hasattr(child, "parameter_constraint")
