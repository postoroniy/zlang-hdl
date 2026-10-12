from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.types import BitsType
from zlang.semantic import SemanticError


def _compile(source: str):
    return compile_source(source).ir


def _contains_bitcast(value: object) -> bool:
    """Search frozen IR records without depending on a particular container path."""

    seen: set[int] = set()

    def visit(item: object) -> bool:
        if isinstance(item, expr.Bitcast):
            return True
        if item is None or isinstance(item, (str, bytes, int, float, bool, Enum)):
            return False
        identity = id(item)
        if identity in seen:
            return False
        seen.add(identity)
        if isinstance(item, (tuple, list)):
            return any(visit(element) for element in item)
        if isinstance(item, dict):
            return any(visit(element) for element in item.values())
        if is_dataclass(item):
            return any(visit(getattr(item, field.name)) for field in fields(item))
        return False

    return visit(value)


@pytest.mark.parametrize(
    ("boundary", "source"),
    (
        (
            "typed local and output",
            "module M { in raw:bits<8> out y:u8 converted:u8=raw y=raw }",
        ),
        (
            "register next state",
            "module M { clock c reset r in raw:bits<8> out y:u8 "
            "reg q:u8=0 q <- raw y=q }",
        ),
        (
            "rule register write",
            "module M { clock c reset r in raw:bits<8> in go:bit out y:u8 "
            "reg q:u8=0 rule load when go { q <- raw } y=q }",
        ),
        (
            "struct field",
            "struct S { value:u8 } module M { in raw:bits<8> out y:S "
            "y=S{value=raw} }",
        ),
        (
            "child scalar binding",
            "module Child { in x:u8 out y:u8 y=x } "
            "module M { in raw:bits<8> out y:u8 inst child:Child{x=raw} y=child.y }",
        ),
        (
            "FIFO rule push",
            "module M { clock c reset r in raw:bits<8> in go:bit out y:u8 "
            "fifo q:fifo<u8,2> rule put when go { q.push(raw) } y=q.front }",
        ),
        (
            "memory write data",
            "module M { clock c reset r in raw:bits<8> in we:bit in a:u1 "
            "out y:u8 memory m:mem<u8,2>{read_latency 1 collision read_first} "
            "m.read_address=a m.write_enable=we m.write_address=a "
            "m.write_data=raw y=m.read_data }",
        ),
        (
            "declared function return",
            "fn decode(x:bits<8>)->u8{x} "
            "module M { in raw:bits<8> out y:u8 y=decode(raw) }",
        ),
    ),
)
def test_implicit_raw_bitcast_is_inserted_at_each_frozen_typed_boundary(
    boundary: str, source: str
) -> None:
    module = _compile(source)
    assert _contains_bitcast(module), boundary


def test_register_initial_raw_boundary_accepts_a_constant_raw_expression() -> None:
    module = _compile(
        "module M { clock c reset r out y:u8 "
        "reg q:u8=concat(pack(0xA),pack(0xF)) y=q }"
    )
    initial = module.registers[0].initial
    assert isinstance(initial, expr.Bitcast)
    assert initial.type.width == 8
    assert isinstance(initial.expression, expr.Concat)


def test_inferred_local_keeps_its_raw_type_until_a_typed_output_boundary() -> None:
    module = _compile(
        "module M { in raw:bits<8> out y:u8 inferred=raw y=inferred }"
    )
    # Immutable inferred locals inline into their consumer. The cast therefore
    # wraps the raw InputRef only at the typed output instead of changing the
    # local's inferred expression type.
    output = module.assignments[0].expression
    assert isinstance(output, expr.Bitcast)
    assert isinstance(output.expression, expr.InputRef)
    assert output.expression.type == BitsType(8)


@pytest.mark.parametrize(
    ("boundary", "source", "message"),
    (
        (
            "ordinary function argument",
            "fn f(x:u8)->u8{x} module M { in raw:bits<8> out y:u8 y=f(raw) }",
            "argument",
        ),
        (
            "operator typing",
            "module M { in raw:bits<8> in x:u8 out y:u9 y=raw+x }",
            "cannot add operands with different type families",
        ),
        (
            "overload resolution",
            "struct Box { value:u8 } "
            "operator +(a:Box,b:Box){Box{value=a.value}} "
            "module M { in a:Box in raw:bits<8> out y:Box y=a+raw }",
            "operator '+'",
        ),
        (
            "declared operator return",
            "struct Box { value:u8 } "
            "operator +(a:Box,b:Box)->u8{pack(a.value)} "
            "module M { in a:Box in b:Box out y:u8 y=a+b }",
            "returns bits<8>, expected u8",
        ),
        (
            "switch branch unification",
            "module M { in select:bit in raw:bits<8> in x:u8 out y:bits<8> "
            "y=switch select { 0=>raw else=>x } }",
            "switch",
        ),
        (
            "protocol compatibility",
            "module M { in rx:rv<bits<8>> out tx:rv<u8> connect rx -> tx }",
            "payload",
        ),
        (
            "memory addressing",
            "module M { clock c reset r in raw:bits<1> in we:bit in data:u8 "
            "out y:u8 memory m:mem<u8,2>{read_latency 1 collision read_first} "
            "m.read_address=raw m.write_enable=we m.write_address=raw "
            "m.write_data=data y=m.read_data }",
            "read_address",
        ),
    ),
)
def test_raw_boundary_conversion_does_not_leak_into_inference_or_resolution(
    boundary: str, source: str, message: str
) -> None:
    with pytest.raises(SemanticError) as caught:
        _compile(source)
    assert message in str(caught.value), boundary
