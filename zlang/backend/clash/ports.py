"""Deterministic public Clash top-port annotations.

Internal Clash component ABIs deliberately retain typed aggregate values.  Only
the selected ``topEntity`` boundary is projected into recursively named struct
leaves so generated RTL is straightforward to integrate with existing HDL.
"""

from __future__ import annotations

from zlang.backend.identifiers import rtl_identifier
from zlang.ir.types import HardwareType, StructType, TupleType


def value_port_annotation(name: str, type_: HardwareType) -> str:
    """Describe one public value port, recursively splitting struct fields.

    Vectors and all scalar/numeric types intentionally remain one physical leaf.
    ``PortProduct`` only affects Clash's selected top-level RTL ABI; the Haskell
    circuit and every closed child component continue to exchange the original
    typed value.
    """

    if not isinstance(type_, (StructType, TupleType)):
        return f'PortName "{rtl_identifier(name)}"'
    return _product_port_annotation(name, type_)


def _struct_port_annotation(name: str, type_: StructType) -> str:
    """Render one product level without independently mangling segments.

    Clash joins nested ``PortProduct``/``PortName`` labels with underscores.
    A label such as ``module`` is therefore safe inside ``request_module`` and
    must remain distinct from a sibling already named ``zlang_module``.
    Escaping each segment would collapse both leaves to the same RTL name.
    """

    # Clash represents a one-field product as the field's physical value, not
    # as a product layer.  Preserve the public semantic path by joining the
    # skipped label into the eventual PortName.  This is recursive so nested
    # wrapper records remain valid without inspecting generated HDL.
    if len(type_.fields) == 1:
        field = type_.fields[0]
        return _nested_port_annotation(f"{name}_{field.name}", field.type)
    fields = ", ".join(
        _nested_port_annotation(field.name, field.type)
        for field in type_.fields
    )
    return f'PortProduct "{name}" [{fields}]'


def _tuple_port_annotation(name: str, type_: TupleType) -> str:
    # Clash inserts one underscore between every PortProduct/PortName segment.
    # Prefix positional labels with one additional underscore so the generated
    # core port exactly matches the frozen public tuple separator:
    # ``p : (u8,bit)`` becomes ``p__item0``/``p__item1``.  This also keeps a
    # distinct ordinary port named ``p_item0`` out of the tuple namespace.
    fields = ", ".join(
        _nested_port_annotation(f"_item{index}", element)
        for index, element in enumerate(type_.elements)
    )
    return f'PortProduct "{name}" [{fields}]'


def _product_port_annotation(
    name: str, type_: StructType | TupleType
) -> str:
    if isinstance(type_, StructType):
        return _struct_port_annotation(name, type_)
    return _tuple_port_annotation(name, type_)


def _nested_port_annotation(name: str, type_: HardwareType) -> str:
    if isinstance(type_, StructType):
        return _struct_port_annotation(name, type_)
    if isinstance(type_, TupleType):
        return _tuple_port_annotation(name, type_)
    return f'PortName "{name}"'


def forward_port_annotation(
    name: str,
    payload_type: HardwareType,
    *control_names: str,
) -> str:
    """Describe a forward protocol value with a recursively split payload."""

    members = [_nested_port_annotation("payload", payload_type)]
    members.extend(
        f'PortName "{control}"' for control in control_names
    )
    return (
        f'PortProduct "{name}" '
        f'[{", ".join(members)}]'
    )


__all__ = ["forward_port_annotation", "value_port_annotation"]
