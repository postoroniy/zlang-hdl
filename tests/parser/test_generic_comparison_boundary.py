# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""A compact <= comparison must not become an uppercase generic type token."""

import pytest
from zlang.parser import parse
from zlang.compiler import compile_source
from zlang.semantic import SemanticError


@pytest.mark.parametrize(
    "condition",
    [
        "W>=1 && W<=32 && CW>=1 && CW<=32",
        "W<=32 && CW>=1",
        "(W<=32) && (CW>=1)",
        "W<=CW && CW>=1",
    ],
)
def test_generic_comparison_spacing_does_not_change_ast(condition):
    compact = f"module M<W,CW> where {condition} {{in x:u8 out y:u8 y=x}}"
    spaced = compact.replace(">=", " >= ").replace("<=", " <= ")
    a, b = parse(compact), parse(spaced)
    assert a.parameter_constraint == b.parameter_constraint


@pytest.mark.parametrize(
    "condition", ["W<=32 && CW>=1", "W>=1 && W<=32 && CW>=1 && CW<=32"]
)
def test_compact_constraint_specializes_normally(condition):
    text = f"module M<W,CW> where {condition} {{in x:u8 out y:u8 y=x}}\n"
    text += "module Top{in x:u8 out y:u8 unit:M<W=8,CW=4> unit.x=x y=unit.y}"
    compile_source(text, top="Top")
    with pytest.raises(SemanticError):
        compile_source(text.replace("W=8", "W=40"), top="Top")
