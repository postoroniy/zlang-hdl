# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Typed callable references preserve child contracts and fail-closed binding."""
from dataclasses import replace

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.native_simulation import simulate
from zlang.opt import lower
from zlang.opt.identity import canonical_ir_identity
from zlang.semantic import SemanticError


SIMPLE = """fn identity(x:u8)->u8{x}
module Apply<op:fn(u8)->u8>{in x:u8 out y:u8 y=op(x)}
module Top {in x:u8 out y:u8
 child:Apply<op=fn identity>
 child.x=x y=child.y
}
"""
GENERIC = SIMPLE.replace("fn identity(x:u8)->u8", "fn identity<type A>(x:A)->A").replace(
    "op=fn identity>", "op=fn identity<A=u8>>",
)
FORWARDED = """fn identity(x:u8)->u8{x}
module Apply<op:fn(u8)->u8>{in x:u8 out y:u8 y=op(x)}
module Forward<op:fn(u8)->u8>{in x:u8 out y:u8
 child:Apply<op=fn op> child.x=x y=child.y }
module Top {in x:u8 out y:u8
 child:Forward<op=fn identity> child.x=x y=child.y }
"""
ARRAY = """fn identity(x:u8)->u8{x}
module Apply<op:fn(u8)->u8>{in x:u8 out y:u8 y=op(x)}
module Top {in values:vec<2,u8> out values_out:vec<2,u8>
 child[2]:Apply<op=fn identity>
 generate(i in 0..2) { child[i].x=values[i] }
 values_out=generate(i in 0..2) child[i].y }
"""


def _source_text(source, origin):
    lines = source.splitlines(keepends=True)
    span = origin.span
    start = sum(map(len, lines[:span.start_line - 1])) + span.start_column - 1
    end = sum(map(len, lines[:span.end_line - 1])) + span.end_column - 1
    return source[start:end]


@pytest.mark.parametrize("source", (SIMPLE, GENERIC, FORWARDED, ARRAY,
                                    SIMPLE.replace("fn identity>", "/* marker, fn */fn identity>")),
                         ids=("direct", "specialized", "forwarded", "array", "commented"))
def test_concise_callable_instances_match_explicit_selected_products(source):
    explicit = source.replace(" child:", " inst child:").replace(" child[2]:", " inst child[2]:")
    before = compile_source(explicit, top="Top").ir
    after = compile_source(source, top="Top").ir
    assert canonical_ir_identity(lower(before)) == canonical_ir_identity(lower(after))
    old_artifact, new_artifact = emit_artifact(before), emit_artifact(after)
    assert old_artifact.text == new_artifact.text
    # Removing `inst` changes source columns, not binding facts. Validate each
    # original range before comparing manifest bytes with those ranges rebound.
    bindings = []
    for old, new in zip(old_artifact.bindings, new_artifact.bindings, strict=True):
        if old.source_origin is not None and new.source_origin is not None:
            assert _source_text(explicit, old.source_origin) == _source_text(source, new.source_origin)
        else:
            assert old.source_origin == new.source_origin
        bindings.append(replace(old, source_origin=new.source_origin))
    assert replace(old_artifact, bindings=tuple(bindings)).to_json() == new_artifact.to_json()
    assert [(x.specialization_identity, x.instance_identity) for x in before.elaborated_instances] == [
        (x.specialization_identity, x.instance_identity) for x in after.elaborated_instances
    ]
    if source == ARRAY:
        inputs = ({"values": (7, 255)}, {"values": (0, 12)})
        expected = [{"values_out": [7, 255]}, {"values_out": [0, 12]}]
    else:
        inputs = ({"x": 7}, {"x": 255})
        expected = [{"y": 7}, {"y": 255}]
    assert [simulate(after, **values) for values in inputs] == expected


@pytest.mark.parametrize("actual,message", (
    ("identity", "requires an explicit 'fn name' reference"),
    ("fn absent", "unknown"),
    ("fn wrong", "expects"),
))
@pytest.mark.parametrize("keyword", ("", "inst "), ids=("concise", "explicit"))
def test_callable_binding_remains_fail_closed(actual, message, keyword):
    source = SIMPLE.replace("fn identity(x:u8)->u8{x}",
                            "fn identity(x:u8)->u8{x} fn wrong(x:u9)->u9{x}")
    source = source.replace(" child:Apply<op=fn identity>", f" {keyword}child:Apply<op={actual}>")
    with pytest.raises(SemanticError, match=message):
        compile_source(source, top="Top")
