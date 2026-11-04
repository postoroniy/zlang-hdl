# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Concise callable actuals use the explicit grammar and original coordinates."""
from dataclasses import fields, is_dataclass
from concurrent.futures import ThreadPoolExecutor

import pytest

from zlang.ast import nodes as ast
from zlang.parser import ParseError, parse
from zlang.source import SourceSpan


@pytest.mark.parametrize("arguments", (
    "op=fn identity",
    "op=fn identity<A=u8>",
    "T=u8,N=2,op=fn choose<A=u8,B=s8>",
    "T=vec<2,u8>,op=fn identity",
    "op=fn outer<inner=fn identity>",
    "op=fn\tchoose<A=u8, B=s8>, N=2",
    "op=fn/* a,b */ identity",
    "op=/* leading */fn identity",
    "op=fn// note\n identity",
))
@pytest.mark.parametrize("array", ("", "[2]"), ids=("scalar", "array"))
def test_concise_callable_actuals_match_explicit_ast(arguments, array):
    concise = parse(f"module M {{ c{array}:Child<{arguments}> }}")
    explicit = parse(f"module M {{ inst c{array}:Child<{arguments}> }}")
    declaration = concise.generic_declarations[0]
    assert declaration.specializations == explicit.instances[0].arguments
    actual = next(arg.value for arg in declaration.specializations if arg.name == "op")
    assert isinstance(actual, ast.CallableRef)
    assert declaration.array_length == explicit.instances[0].array_length


def test_reparsed_argument_spans_belong_to_the_original_multiline_source():
    source = "module M {\n  child:Child<T=vec<2,u8>,\n    op=fn choose<A=u8,B=s8>>\n}\n"
    args = parse(source).generic_declarations[0].specializations
    rows = []

    def visit(value):
        if is_dataclass(value):
            if isinstance(value, ast.TypeName) and value.origin is not None:
                span = value.origin
                line = source.splitlines()[span.start_line - 1]
                assert span.start_line == span.end_line
                assert line[span.start_column - 1:span.end_column - 1] == value.text
                rows.append(value.text)
            for field in fields(value):
                if field.name not in {"origin", "named_origins"}:
                    visit(getattr(value, field.name))
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item)
    visit(args)
    assert sorted(rows) == ["s8", "u8", "u8"]
    vector = args[0].value
    assert isinstance(vector, ast.VectorTypeName)
    assert isinstance(vector.origin, SourceSpan)
    line = source.splitlines()[vector.origin.start_line - 1]
    assert line[vector.origin.start_column - 1:vector.origin.end_column - 1] == "vec<2,u8>"


@pytest.mark.parametrize("actual", ("fn", "fn identity<>", "fn 123", "fn identity<A=>",
                                    "fn identity<A=u8,,B=s8>", "fn(identity)"))
def test_malformed_callable_actual_is_a_located_parse_error(actual):
    source = f"module M {{\n child:Child<op={actual}>\n}}"
    with pytest.raises(ParseError) as raised:
        parse(source)
    assert raised.value.primary is not None
    assert raised.value.primary.span.start_line == 2
    assert "__Concise" not in str(raised.value)


def test_nonkeyword_fn_substrings_keep_legacy_named_argument_ast():
    args = parse("module M { c:C<T=fn_type,N=2> }").generic_declarations[0].specializations
    assert args == (ast.SpecializationArgument("T", "fn_type"), ast.SpecializationArgument("N", 2))


def test_concurrent_reparse_has_no_shared_source_coordinates():
    sources = ["\n" * i + "module M { c:C<op=fn choose<A=u8>> }" for i in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(parse, sources))
    for i, syntax in enumerate(results):
        reference = syntax.generic_declarations[0].specializations[0].value
        assert isinstance(reference, ast.CallableRef)
        assert reference.specializations[0].value.origin.start_line == i + 1
