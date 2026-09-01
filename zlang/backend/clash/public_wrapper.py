"""Typed public SystemVerilog boundary for Clash-generated RTL.

Clash deliberately retains ``Vec`` as a packed Verilog value at an annotated
top entity.  ZLang's public physical ABI instead exposes vectors as native
unpacked arrays and recursively exposes struct members.  This module bridges
those two already-typed representations without inspecting generated RTL or
guessing protocol-specific signal names.

The Clash component prefix is part of the invocation contract: the generated
core is named ``<prefix>_<top>`` and this artifact publishes the selected ZLang
top name around it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from zlang.backend.identifiers import (
    allocate_private_rtl_identifier,
    rtl_identifier,
)
from zlang.backend.manifest import BackendArtifact
from zlang.ir.module import Module, PortDirection
from zlang.ir.physical_types import PhysicalSignedness, physical_signedness
from zlang.ir.top_abi import ExternalTopLeaf, TopPhysicalABI
from zlang.ir.types import HardwareType, StructType, TupleType


class ClashPublicWrapperError(ValueError):
    """A typed public ABI cannot be connected to the Clash core ABI."""


@dataclass(frozen=True)
class _PublicElement:
    leaf: ExternalTopLeaf
    expression: str
    msb: int
    lsb: int


@dataclass(frozen=True)
class _CorePort:
    name: str
    type: HardwareType
    direction: PortDirection
    elements: tuple[_PublicElement, ...]

    @property
    def width(self) -> int:
        return self.type.width


@dataclass(frozen=True)
class ClashPublicTopWrapper:
    """One deterministic typed wrapper companion for a Clash top entity."""

    module_name: str
    core_prefix: str
    abi: TopPhysicalABI
    text: str
    logical_path: str

    @property
    def core_module_name(self) -> str:
        return f"{self.core_prefix}_{rtl_identifier(self.module_name)}"

    @classmethod
    def build(
        cls,
        module: Module,
        *,
        core_prefix: str = "zlang_core",
    ) -> "ClashPublicTopWrapper":
        """Build the selected-top wrapper from backend-independent typed IR."""

        if not core_prefix or rtl_identifier(core_prefix) != core_prefix:
            raise ClashPublicWrapperError(
                "Clash public wrapper core prefix must be a safe HDL identifier"
            )
        top_name = rtl_identifier(module.name)
        # Clash applies the prefix to the explicit Synthesize.t_name.  Module
        # source names are already valid ZLang identifiers; fail closed if a
        # future source spelling would require backend-only renaming.
        if top_name != module.name:
            raise ClashPublicWrapperError(
                f"Clash selected top name '{module.name}' is not a safe public HDL identifier"
            )
        abi = module.top_physical_abi
        ports = _core_ports(abi)
        text = _render_wrapper(module.name, core_prefix, abi, ports)
        return cls(
            module_name=module.name,
            core_prefix=core_prefix,
            abi=abi,
            text=text,
            logical_path=f"{module.name}.sv",
        )


def bind_artifact_to_public_wrapper(
    artifact: BackendArtifact,
    wrapper: ClashPublicTopWrapper,
) -> BackendArtifact:
    """Publish leaf locators only after the typed wrapper exists.

    The BackendArtifact continues to identify the generated Clash source; the
    whole-build manifest separately hashes the final core and wrapper RTL.
    This bounded adapter changes only physical locator availability and never
    fabricates a locator for a semantic root hidden by the public boundary.
    """

    if artifact.backend != "clash" or artifact.module != wrapper.module_name:
        raise ClashPublicWrapperError(
            "Clash artifact does not match its typed public wrapper"
        )
    leaves = {leaf.leaf_semantic_id: leaf for leaf in wrapper.abi.leaves}
    rebound = []
    seen: set[str] = set()
    for binding in artifact.bindings:
        leaf = leaves.get(binding.semantic_signal_id)
        if leaf is None:
            rebound.append(binding)
            continue
        seen.add(binding.semantic_signal_id)
        rebound.append(replace(
            binding,
            rtl_module=wrapper.module_name,
            rtl_path=rtl_identifier(leaf.external_name),
            physical_available=True,
        ))
    missing = tuple(sorted(set(leaves) - seen))
    if missing:
        raise ClashPublicWrapperError(
            "Clash artifact is missing public wrapper bindings: "
            + ", ".join(missing)
        )
    return replace(artifact, bindings=tuple(rebound))


def _render_wrapper(
    module_name: str,
    core_prefix: str,
    abi: TopPhysicalABI,
    core_ports: tuple[_CorePort, ...],
) -> str:
    public_ports = tuple(_public_declaration(leaf) for leaf in abi.leaves)
    if not public_ports:
        raise ClashPublicWrapperError(
            f"Clash public top '{module_name}' has no physical ports"
        )

    public_names = {rtl_identifier(leaf.external_name) for leaf in abi.leaves}
    private_names = set(public_names)
    core_instance = allocate_private_rtl_identifier(
        "zlang_top_core",
        semantic_identity=f"{module_name}:public-wrapper:core-instance",
        used=private_names,
    )
    declarations: list[str] = []
    assignments: list[str] = []
    connections: list[str] = []
    for port in core_ports:
        core_signal = allocate_private_rtl_identifier(
            f"zlang_top_core_{rtl_identifier(port.name)}",
            semantic_identity=f"{module_name}:public-wrapper:core-port:{port.name}",
            used=private_names,
        )
        if port.direction is PortDirection.INPUT:
            connections.append(
                f"    .{rtl_identifier(port.name)}({_input_value(port)})"
            )
            continue

        declarations.append(
            f"  wire {_packed_range(port.width)}{core_signal};"
        )
        connections.append(
            f"    .{rtl_identifier(port.name)}({core_signal})"
        )
        for element in port.elements:
            value = _packed_slice(core_signal, element.msb, element.lsb, port.width)
            assignments.append(f"  assign {element.expression} = {value};")

    top_name = rtl_identifier(module_name)
    core_name = f"{core_prefix}_{top_name}"
    port_text = ",\n".join(f"  {port}" for port in public_ports)
    connection_text = ",\n".join(connections)
    body = "\n".join((
        *declarations,
        f"  {core_name} {core_instance} (\n{connection_text}\n  );",
        *assignments,
    ))
    return (
        "// ZLang typed public wrapper around the packed Clash core.\n"
        "`default_nettype none\n"
        "`timescale 100fs/100fs\n"
        f"module {top_name} (\n{port_text}\n);\n"
        f"{body}\n"
        "endmodule\n"
        "`default_nettype wire\n"
    )


def _public_declaration(leaf: ExternalTopLeaf) -> str:
    direction = "input wire" if leaf.direction is PortDirection.INPUT else "output wire"
    signed = (
        " signed"
        if physical_signedness(leaf.element_type) is PhysicalSignedness.SIGNED
        else ""
    )
    dimensions = "".join(f" [0:{length - 1}]" for length in leaf.array_dimensions)
    return (
        f"{direction}{signed} {_packed_range(leaf.element_width)}"
        f"{rtl_identifier(leaf.external_name)}{dimensions}"
    )


def _core_ports(abi: TopPhysicalABI) -> tuple[_CorePort, ...]:
    roots: dict[str, list[ExternalTopLeaf]] = {}
    root_order: list[str] = []
    for leaf in abi.leaves:
        root = leaf.packed_root_semantic_id
        if root is None or leaf.packed_root_type is None:
            raise ClashPublicWrapperError(
                f"public leaf '{leaf.leaf_semantic_id}' has no typed packed root"
            )
        if root not in roots:
            roots[root] = []
            root_order.append(root)
        roots[root].append(leaf)

    ports: list[_CorePort] = []
    names: set[str] = set()
    for root in root_order:
        group = tuple(roots[root])
        first = group[0]
        if any(leaf.direction is not first.direction for leaf in group):
            raise ClashPublicWrapperError(
                f"packed root '{root}' contains mixed physical directions"
            )

        # Aggregate hierarchy emission already consumes TopPhysicalABI leaves
        # directly.  Every aggregate leaf is therefore one Clash PortName,
        # even when the leaf itself is an unpacked public vector.
        if first.category == "aggregate":
            for leaf in group:
                port = _leaf_core_port(leaf)
                _append_unique(ports, names, port)
            continue

        prefix = _root_member_prefix(first)
        root_type = first.packed_root_type
        assert root_type is not None
        for path, type_, base_lsb in _struct_terminals(root_type):
            related = tuple(
                leaf
                for leaf in group
                if _relative_member_path(leaf, prefix)[:len(path)] == path
            )
            if not related:
                raise ClashPublicWrapperError(
                    f"Clash core terminal '{root}.{'.'.join(path)}' has no public leaves"
                )
            # Clash concatenates PortProduct labels before producing the RTL
            # identifier.  Apply reserved-word mangling once to that complete
            # flattened spelling: a struct root named ``output`` therefore
            # has core leaves ``output_re``/``output_im``, not the nonexistent
            # ``zlang_output_re``/``zlang_output_im``.
            root_name = first.packed_root_external_name or first.external_name
            name = _core_terminal_name(root_name, root_type, path)
            elements: list[_PublicElement] = []
            for leaf in related:
                for item, expression in _leaf_elements(leaf):
                    if item.lsb < base_lsb or item.msb >= base_lsb + type_.width:
                        raise ClashPublicWrapperError(
                            f"public leaf '{leaf.leaf_semantic_id}' escapes Clash core port '{name}'"
                        )
                    elements.append(_PublicElement(
                        leaf,
                        expression,
                        item.msb - base_lsb,
                        item.lsb - base_lsb,
                    ))
            port = _CorePort(
                name,
                type_,
                first.direction,
                _validated_elements(name, type_.width, elements),
            )
            _append_unique(ports, names, port)
    return tuple(ports)


def _leaf_core_port(leaf: ExternalTopLeaf) -> _CorePort:
    """Build a core PortName whose typed value is exactly one public leaf."""

    width = leaf.width
    elements: list[_PublicElement] = []
    # The public leaf's own canonical representation is dense even when its
    # occurrences are interleaved inside an enclosing aggregate packed root.
    for ordinal, (_item, expression) in enumerate(_leaf_elements(leaf)):
        msb = width - ordinal * leaf.element_width - 1
        lsb = msb - leaf.element_width + 1
        elements.append(_PublicElement(leaf, expression, msb, lsb))
    return _CorePort(
        rtl_identifier(leaf.external_name),
        leaf.canonical_type,
        leaf.direction,
        _validated_elements(leaf.external_name, width, elements),
    )


def _append_unique(
    ports: list[_CorePort], names: set[str], port: _CorePort
) -> None:
    if port.name in names:
        raise ClashPublicWrapperError(
            f"duplicate Clash core top port '{port.name}'"
        )
    names.add(port.name)
    ports.append(port)


def _validated_elements(
    port_name: str,
    width: int,
    elements: list[_PublicElement],
) -> tuple[_PublicElement, ...]:
    ordered = tuple(sorted(elements, key=lambda item: item.msb, reverse=True))
    occupied: set[int] = set()
    for item in ordered:
        if item.lsb < 0 or item.msb >= width or item.msb < item.lsb:
            raise ClashPublicWrapperError(
                f"invalid packed slice for Clash core port '{port_name}'"
            )
        bits = set(range(item.lsb, item.msb + 1))
        if occupied & bits:
            raise ClashPublicWrapperError(
                f"overlapping public leaves for Clash core port '{port_name}'"
            )
        occupied.update(bits)
    if occupied != set(range(width)):
        raise ClashPublicWrapperError(
            f"public leaves do not completely cover Clash core port '{port_name}'"
        )
    return ordered


def _struct_terminals(
    type_: HardwareType,
) -> tuple[tuple[tuple[str, ...], HardwareType, int], ...]:
    """Mirror Clash annotations: split structs, stop at every other type."""

    result: list[tuple[tuple[str, ...], HardwareType, int]] = []

    def visit(current: HardwareType, path: tuple[str, ...], base_lsb: int) -> None:
        if not isinstance(current, (StructType, TupleType)):
            result.append((path, current, base_lsb))
            return
        cursor = base_lsb + current.width
        members = (
            tuple((field.name, field.type) for field in current.fields)
            if isinstance(current, StructType)
            else tuple(
                (f"item{index}", element)
                for index, element in enumerate(current.elements)
            )
        )
        for name, member_type in members:
            cursor -= member_type.width
            visit(member_type, path + (name,), cursor)

    visit(type_, (), 0)
    return tuple(result)


def _core_terminal_name(
    root_name: str,
    root_type: HardwareType,
    path: tuple[str, ...],
) -> str:
    """Mirror Clash product separators for one native aggregate terminal."""

    rendered = root_name
    current = root_type
    for segment in path:
        if isinstance(current, StructType):
            field = next(
                (item for item in current.fields if item.name == segment), None
            )
            if field is None:  # pragma: no cover - paired with _struct_terminals
                raise ClashPublicWrapperError(
                    f"unknown Clash core struct terminal '{segment}'"
                )
            rendered += "_" + segment
            current = field.type
            continue
        if isinstance(current, TupleType):
            match = re.fullmatch(r"item([0-9]+)", segment)
            if match is None or int(match.group(1)) >= len(current.elements):
                raise ClashPublicWrapperError(
                    f"invalid Clash core tuple terminal '{segment}'"
                )
            rendered += "__" + segment
            current = current.elements[int(match.group(1))]
            continue
        raise ClashPublicWrapperError(
            f"Clash core terminal path traverses scalar type '{current}'"
        )
    # Apply reserved-word mangling once to the complete physical identifier,
    # exactly as for the historical struct-only path.
    return rtl_identifier(rendered)


def _root_member_prefix(leaf: ExternalTopLeaf) -> tuple[str, ...]:
    if leaf.category in {"clock", "reset"}:
        return leaf.member_path
    if leaf.category == "port":
        return leaf.member_path[:1] if leaf.signal_kind == "wire" else leaf.member_path[:2]
    if leaf.category == "request_response":
        return leaf.member_path[:3]
    if leaf.category == "aggregate":
        return leaf.member_path[:2] if leaf.signal_kind == "wire" else leaf.member_path[:3]
    raise ClashPublicWrapperError(
        f"unsupported public leaf category '{leaf.category}'"
    )


def _relative_member_path(
    leaf: ExternalTopLeaf,
    prefix: tuple[str, ...],
) -> tuple[str, ...]:
    if leaf.member_path[:len(prefix)] != prefix:
        raise ClashPublicWrapperError(
            f"public leaf '{leaf.leaf_semantic_id}' does not share its typed root path"
        )
    return leaf.member_path[len(prefix):]


def _leaf_elements(leaf: ExternalTopLeaf):
    name = rtl_identifier(leaf.external_name)
    return tuple(
        (
            item,
            name + "".join(f"[{index}]" for index in item.indices),
        )
        for item in leaf.packed_element_slices
    )


def _input_value(port: _CorePort) -> str:
    values = tuple(item.expression for item in port.elements)
    return values[0] if len(values) == 1 else "{" + ", ".join(values) + "}"


def _packed_range(width: int) -> str:
    if width < 1:
        raise ClashPublicWrapperError("public physical widths must be positive")
    return "" if width == 1 else f"[{width - 1}:0] "


def _packed_slice(signal: str, msb: int, lsb: int, width: int) -> str:
    if width == 1 and msb == 0 and lsb == 0:
        return signal
    if msb == lsb:
        return f"{signal}[{msb}]"
    return f"{signal}[{msb}:{lsb}]"


__all__ = [
    "ClashPublicTopWrapper",
    "ClashPublicWrapperError",
    "bind_artifact_to_public_wrapper",
]
