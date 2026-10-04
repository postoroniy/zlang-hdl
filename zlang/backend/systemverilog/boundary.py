"""Validated public-port and packed top-boundary SystemVerilog ownership."""

from __future__ import annotations

from dataclasses import dataclass, replace

from zlang.common import stable_digest
from zlang.ir import packing as ir_packing
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.backend import identifiers as identifiers
from zlang.backend import naming as naming
from zlang.backend.systemverilog import context as emission_context
from zlang.ir.top_abi import build_top_physical_abi
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import rendering as sv_rendering


_PROTOCOL_SIGNAL_KINDS = {
    ir_interfaces.InterfaceProtocol.READY_VALID: ("payload", "valid", "ready"),
    ir_interfaces.InterfaceProtocol.CREDIT: ("payload", "send", "return"),
    ir_interfaces.InterfaceProtocol.VC_CREDIT: (
        "payload", "vc", "send", "return", "return_vc",
    ),
}


def top_physical_rtl_names(module: ir_module.Module) -> dict[str, str]:
    """Return the one authoritative direct-SV public-name projection.

    Both production and formal-only artifacts describe the same public
    ``TopPhysicalABI``.  Derive every physical token through the emitter's
    existing identifier policy so a formal harness can never reconstruct an
    unmangled ZLang spelling or maintain a second reserved-word list.
    """

    names = {
        f"port:{port.name}": sv_rendering._identifier(port.name) for port in module.ports
    }
    for port in module.ports:
        base = sv_rendering._identifier(port.name)
        names.update({
            f"port:{port.name}.{signal}": f"{base}_{signal}"
            for signal in _PROTOCOL_SIGNAL_KINDS.get(port.protocol, ())
        })
    # The complete public TopPhysicalABI is authoritative. Legacy protocol
    # spellings above describe internal packed roots and may differ when a
    # reserved root such as ``input`` is flattened into a public leaf such as
    # ``input_payload``.
    names.update({
        leaf.leaf_semantic_id: sv_rendering._identifier(leaf.external_name)
        for leaf in module.top_physical_abi.leaves
    })
    if module.clock:
        names["clock"] = sv_rendering._identifier(module.clock)
    if module.reset:
        names["reset"] = sv_rendering._identifier(module.reset)
    return names


@dataclass(frozen=True)
class TopBoundaryPlan:
    """One inline public boundary for the selected direct-SV top.

    Component modules retain their compact packed ABI.  Only the selected top
    uses this plan: public aggregate leaves remain real module ports while the
    existing body consumes deterministic private packed aliases in the same
    module.  No wrapper module or hierarchy instance is involved.
    """

    module_name: str
    type_declarations: tuple[str, ...]
    public_ports: tuple[str, ...]
    base_aliases: tuple[tuple[str, str], ...]
    direct_vector_bases: tuple[str, ...]
    declarations: tuple[str, ...]
    input_bridges: tuple[str, ...]
    output_bridges: tuple[str, ...]

    @property
    def identity(self) -> str:
        """Stable physical identity of the validated inline boundary."""

        payload: dict[str, object] = {
            "schema": (
                "zlang-direct-sv-top-boundary-v4"
                if self.type_declarations else
                "zlang-direct-sv-top-boundary-v3"
                if self.direct_vector_bases else
                "zlang-direct-sv-top-boundary-v2"
            ),
            "packing_layout_schema": ir_packing.PACKING_LAYOUT_SCHEMA,
            "module": self.module_name,
            "public_ports": list(self.public_ports),
            "base_aliases": [list(item) for item in self.base_aliases],
            "declarations": list(self.declarations),
            "input_bridges": list(self.input_bridges),
            "output_bridges": list(self.output_bridges),
        }
        if self.type_declarations:
            payload["type_declarations"] = list(self.type_declarations)
        if self.direct_vector_bases:
            payload["direct_vector_bases"] = list(self.direct_vector_bases)
        return stable_digest(payload)

    @property
    def physical_module_name(self) -> str:
        return identifiers.rtl_identifier(self.module_name)

    def alias(self, source_name: str) -> str | None:
        return dict(self.base_aliases).get(source_name)

    def is_direct_vector(self, source_name: str) -> bool:
        """Return whether one source root keeps its packed vector shape."""

        return source_name in self.direct_vector_bases


def _physical_port_declarations(module: ir_module.Module) -> list[str]:
    ports: list[str] = _clock_reset_port_declarations(module)
    for port in module.ports:
        name = sv_rendering._identifier(port.name)
        if port.protocol is ir_interfaces.InterfaceProtocol.WIRE:
            ports.append(_logic_port("input" if port.direction is ir_module.PortDirection.INPUT else "output", name, port.type))
        elif port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            if port.direction is ir_module.PortDirection.INPUT:
                ports.extend((_logic_port("input", f"{name}_payload", port.type),
                              f"input wire logic {name}_valid", f"output logic {name}_ready"))
            else:
                ports.extend((_logic_port("output", f"{name}_payload", port.type),
                              f"output logic {name}_valid", f"input wire logic {name}_ready"))
        else:
            raise SystemVerilogEmissionError(
                f"composed direct SystemVerilog does not support {port.protocol.value} port '{port.name}'"
            )
    for interface in module.request_responses:
        name = sv_rendering._identifier(interface.name)
        requester = interface.role is not None and interface.role.value == "requester"
        request_out = requester
        response_out = not requester
        ports.extend((
            _logic_port("output" if request_out else "input", f"{name}_request_payload", interface.request_type),
            f"{'output' if request_out else 'input'} logic {name}_request_valid",
            f"{'input' if request_out else 'output'} logic {name}_request_ready",
            _logic_port("output" if response_out else "input", f"{name}_response_payload", interface.response_type),
            f"{'output' if response_out else 'input'} logic {name}_response_valid",
            f"{'input' if response_out else 'output'} logic {name}_response_ready",
        ))
    return ports


def _clock_reset_port_declarations(module: ir_module.Module) -> list[str]:
    """Render every explicit physical domain exactly once.

    ``module.clock``/``module.reset`` are the legacy single-domain aliases.
    Multi-domain modules intentionally leave those aliases unset, so backend
    ports must be derived from the shared ``TopPhysicalABI`` rather than from
    the aliases or an unused clock/reset would silently disappear.
    """

    return [
        f"input wire logic {sv_rendering._identifier(leaf.external_name)}"
        for leaf in module.top_physical_abi.leaves
        if leaf.signal_kind in {"clock", "reset"}
    ]


def _public_leaf_port_declaration(leaf: object) -> str:
    """Render one public top leaf, preserving vectors as packed SV arrays."""

    direction = (
        "input" if leaf.direction is ir_module.PortDirection.INPUT else "output"
    )
    name = sv_rendering._identifier(leaf.external_name)
    element = leaf.element_type
    net = " wire" if direction == "input" else ""
    dimensions = "".join(
        f"[{length - 1}:0]" for length in leaf.array_dimensions
    )
    if isinstance(element, ir_types.TaggedUnionType):
        return (
            f"{direction}{net} {_tagged_union_boundary_type_name(element)}"
            f"{' ' + dimensions if dimensions else ''} {name}"
        )
    if not leaf.array_dimensions:
        return _logic_port(direction, name, leaf.canonical_type)
    signed = " signed" if isinstance(element, (ir_types.SIntType, ir_types.FixedType)) else ""
    element_range = sv_rendering._range(sv_rendering._width(element)).strip()
    return f"{direction}{net} logic{signed} {dimensions}{element_range} {name}"


def _tagged_union_boundary_type_name(
    type_: ir_types.TaggedUnionType,
) -> str:
    identity = stable_digest({
        "schema": "zlang-direct-sv-tagged-union-boundary-v1",
        "name": type_.name,
        "declaration_identity": type_.declaration_identity,
    })[:10]
    return f"zlang_tagged_union_{identifiers.rtl_identifier(type_.name)}_{identity}_t"


def _top_boundary_type_declarations(
    leaves: tuple[object, ...],
) -> tuple[str, ...]:
    unions = {
        leaf.element_type for leaf in leaves
        if isinstance(leaf.element_type, ir_types.TaggedUnionType)
    }
    declarations: dict[str, str] = {}
    for type_ in unions:
        name = _tagged_union_boundary_type_name(type_)
        fields = [f"  logic {sv_rendering._range(type_.tag_width)}tag;"]
        if type_.payload_width:
            fields.append(f"  logic {sv_rendering._range(type_.payload_width)}payload;")
        if name in declarations:
            raise SystemVerilogEmissionError(
                "tagged-union boundary types have colliding physical names"
            )
        declarations[name] = "\n".join(("typedef struct packed {", *fields,
                                         f"}} {name};"))
    return tuple(declarations[name] for name in sorted(declarations))


def _public_leaf_elements(leaf: object) -> tuple[tuple[object, str], ...]:
    """Return exact packed slices and their public scalar/array expressions."""

    name = sv_rendering._identifier(leaf.external_name)
    return tuple(
        (
            item,
            name + "".join(
                f"[{index}]"
                for length, index in zip(
                    leaf.array_dimensions, item.indices, strict=True,
                )
            ),
        )
        for item in leaf.packed_element_slices
    )


def _top_boundary_source_base(leaf: object) -> str:
    """Return the source-owned base which names one compact packed root."""

    if leaf.category in {"clock", "reset"}:
        return str(leaf.external_name)
    if leaf.category in {"port", "request_response"}:
        return str(leaf.member_path[0])
    if leaf.category == "aggregate":
        endpoint, member, *_ = leaf.member_path
        return f"{endpoint}__{member}"
    if leaf.packed_root_external_name:
        return str(leaf.packed_root_external_name)
    raise SystemVerilogEmissionError(
        f"public top ABI leaf '{leaf.leaf_semantic_id}' has no packed root base"
    )


def _top_boundary_signal(leaf: object) -> str:
    """Map one typed physical-ABI root to its containing-module signal."""

    if not leaf.packed_root_external_name:
        raise SystemVerilogEmissionError(
            f"public top ABI leaf '{leaf.leaf_semantic_id}' has no private root"
        )
    if leaf.category in {"clock", "reset"}:
        return sv_rendering._identifier(leaf.packed_root_external_name)
    if leaf.category == "request_response":
        interface, channel, signal, *_ = leaf.member_path
        return f"{sv_rendering._identifier(interface)}_{channel}_{signal}"
    if leaf.category in {"port", "aggregate"}:
        base = sv_rendering._identifier(_top_boundary_source_base(leaf))
        return (
            base if leaf.signal_kind == "wire"
            else f"{base}_{leaf.signal_kind}"
        )
    return sv_rendering._identifier(leaf.packed_root_external_name)


def _validate_public_leaf_identifiers(leaves: tuple[object, ...]) -> None:
    """Reject public names which collide after physical HDL mangling.

    ``TopPhysicalABI`` validates logical external names.  The direct-SV
    reserved-word policy is a second, backend-specific namespace projection:
    for example ``module`` and ``zlang_module`` are distinct ZLang names but
    both become ``zlang_module`` in RTL.  Validate that projection before any
    artifact can be published.
    """

    collision = identifiers.first_rtl_leaf_identifier_collision(leaves)
    if collision is not None:
        raise SystemVerilogEmissionError(
            "direct-SystemVerilog public top identifier collision after "
            f"mangling: '{collision.first_external_name}' "
            f"({collision.first_semantic_id}) and "
            f"'{collision.second_external_name}' "
            f"({collision.second_semantic_id}) both map to "
            f"'{collision.physical_name}'"
        )


def _validate_top_boundary_root(root: str, group: list[object]) -> None:
    """Prove that public leaves cover one compact root exactly once."""

    first = group[0]
    if first.packed_root_type is None:
        raise SystemVerilogEmissionError(
            f"public top ABI root '{root}' has no packed type"
        )
    if any(
        leaf.direction is not first.direction
        or leaf.packed_root_type != first.packed_root_type
        or _top_boundary_source_base(leaf)
        != _top_boundary_source_base(first)
        for leaf in group
    ):
        raise SystemVerilogEmissionError(
            f"public top ABI root '{root}' has inconsistent leaf metadata"
        )
    slices = sorted(
        (item for leaf in group for item in leaf.packed_element_slices),
        key=lambda item: (item.lsb, item.msb),
    )
    width = sv_rendering._width(first.packed_root_type)
    if not slices or slices[0].lsb != 0 or slices[-1].msb != width - 1:
        raise SystemVerilogEmissionError(
            f"public top ABI root '{root}' does not cover its exact packed width"
        )
    cursor = 0
    for item in slices:
        if item.lsb != cursor:
            raise SystemVerilogEmissionError(
                f"public top ABI root '{root}' has overlapping or missing slices"
            )
        cursor = item.msb + 1


def _leaf_is_contiguous(leaf: object) -> bool:
    slices = tuple(sorted(
        leaf.packed_element_slices, key=lambda item: item.msb, reverse=True,
    ))
    return bool(slices) and all(
        left.lsb == right.msb + 1
        for left, right in zip(slices, slices[1:])
    )


def _leaf_input_value(leaf: object) -> str:
    name = identifiers.rtl_identifier(leaf.external_name)
    if not leaf.array_dimensions or _leaf_is_contiguous(leaf):
        return name
    return "{" + ", ".join(
        value
        for _item, value in sorted(
            _public_leaf_elements(leaf),
            key=lambda pair: pair[0].msb,
            reverse=True,
        )
    ) + "}"


def _build_top_boundary_plan(
    module: ir_module.Module,
    *,
    leaves: tuple[object, ...] | None = None,
) -> TopBoundaryPlan:
    """Build the selected top's inline leaf/packed-array boundary."""

    selected_leaves = leaves or tuple(build_top_physical_abi(module).leaves)
    _validate_public_leaf_identifiers(selected_leaves)
    type_declarations = _top_boundary_type_declarations(selected_leaves)
    public_ports = tuple(
        _public_leaf_port_declaration(leaf) for leaf in selected_leaves
    )
    roots: dict[str, list[object]] = {}
    root_order: list[str] = []
    for leaf in selected_leaves:
        if leaf.signal_kind in {"clock", "reset"}:
            continue
        root = leaf.packed_root_semantic_id
        if root is None:
            raise SystemVerilogEmissionError(
                f"public top ABI leaf '{leaf.leaf_semantic_id}' has no packed root"
            )
        if root not in roots:
            roots[root] = []
            root_order.append(root)
        roots[root].append(leaf)
    for root, group in roots.items():
        _validate_top_boundary_root(root, group)

    bases_requiring_alias: set[str] = set()
    direct_vector_bases: set[str] = set()
    for root in root_order:
        group = roots[root]
        first = group[0]
        base = _top_boundary_source_base(first)
        direct_vector = (
            len(group) == 1
            and len(first.array_dimensions) == 1
            and isinstance(first.packed_root_type, ir_types.VecType)
            and first.leaf_semantic_id == root
            and identifiers.rtl_identifier(first.external_name)
            == _top_boundary_signal(first)
        )
        direct = (
            len(group) == 1
            and (not first.array_dimensions or direct_vector)
            and first.leaf_semantic_id == root
            and identifiers.rtl_identifier(first.external_name)
            == _top_boundary_signal(first)
        )
        if not direct:
            bases_requiring_alias.add(base)
        elif direct_vector:
            direct_vector_bases.add(base)

    used = set(naming.module_rtl_names(module).allocated_names)
    used.update(identifiers.rtl_identifier(leaf.external_name) for leaf in selected_leaves)
    aliases: dict[str, str] = {}
    for base in sorted(bases_requiring_alias):
        aliases[base] = identifiers.allocate_private_rtl_identifier(
            f"zlang_packed_{base}",
            semantic_identity=f"top-boundary:{module.name}:{base}",
            used=used,
        )

    partial = TopBoundaryPlan(
        module.name,
        type_declarations,
        public_ports,
        tuple(sorted(aliases.items())),
        tuple(sorted(direct_vector_bases)),
        (), (), (),
    )
    declarations: list[str] = []
    input_bridges: list[str] = []
    output_bridges: list[str] = []
    with emission_context.top_boundary_scope(partial):
        for root in root_order:
            group = roots[root]
            first = group[0]
            base = _top_boundary_source_base(first)
            if base not in aliases:
                continue
            signal = _top_boundary_signal(first)
            root_width = sv_rendering._width(first.packed_root_type)
            signed = " signed" if isinstance(
                first.packed_root_type, (ir_types.SIntType, ir_types.FixedType)
            ) else ""
            declarations.append(
                f"  logic{signed} {sv_rendering._range(root_width)}{signal};"
            )
            ordered = sorted(
                group,
                key=lambda leaf: max(
                    item.msb for item in leaf.packed_element_slices
                ),
                reverse=True,
            )
            if first.direction is ir_module.PortDirection.INPUT:
                if all(_leaf_is_contiguous(leaf) for leaf in ordered):
                    parts = [_leaf_input_value(leaf) for leaf in ordered]
                    value = parts[0] if len(parts) == 1 else (
                        "{" + ", ".join(parts) + "}"
                    )
                    input_bridges.append(f"  assign {signal} = {value};")
                else:
                    for leaf in ordered:
                        for item, source in _public_leaf_elements(leaf):
                            input_bridges.append(
                                f"  assign {sv_rendering._slice(signal, item.msb, item.lsb)} "
                                f"= {source};"
                            )
                continue

            for leaf in ordered:
                name = identifiers.rtl_identifier(leaf.external_name)
                elements = _public_leaf_elements(leaf)
                if _leaf_is_contiguous(leaf):
                    msb = max(item.msb for item, _target in elements)
                    lsb = min(item.lsb for item, _target in elements)
                    value = (
                        signal
                        if msb == root_width - 1 and lsb == 0
                        else sv_rendering._slice(signal, msb, lsb)
                    )
                    output_bridges.append(
                        f"  assign {name} = {value};"
                    )
                    continue
                for item, target in elements:
                    output_bridges.append(
                        f"  assign {target} = "
                        f"{sv_rendering._slice(signal, item.msb, item.lsb)};"
                    )
    return replace(
        partial,
        declarations=tuple(declarations),
        input_bridges=tuple(input_bridges),
        output_bridges=tuple(output_bridges),
    )


def physical_state_root_path(module: ir_module.Module) -> tuple[str, ...]:
    """Return the selected top's direct architectural-state VPI root."""

    return ("TOP", identifiers.rtl_identifier(module.name))
def _port_declaration(port: ir_module.Port) -> str:
    direction = "input" if port.direction is ir_module.PortDirection.INPUT else "output"
    return _logic_port(direction, sv_rendering._identifier(port.name), port.type)


def _logic_port(direction: str, name: str, type_: ir_types.HardwareType) -> str:
    signed = " signed" if isinstance(type_, (ir_types.SIntType, ir_types.FixedType)) else ""
    net = " wire" if direction == "input" else ""
    return f"{direction}{net} logic{signed} {sv_rendering._range(sv_rendering._width(type_))}{name}"
