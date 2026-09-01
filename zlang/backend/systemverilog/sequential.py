"""Shared physical clock/reset rendering for direct SystemVerilog."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
import hashlib

from zlang.backend.identifiers import allocate_private_rtl_identifier
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.expressions import Delay, Pipeline
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module


class PhysicalDomainError(ValueError):
    pass


def module_domain(module: Module, clock: str | None = None) -> ClockDomain:
    selected = clock or module.clock
    matches = tuple(
        domain for domain in module.clock_domains if domain.clock == selected
    )
    if len(matches) != 1:
        raise PhysicalDomainError(
            f"module '{module.name}' requires exactly one typed physical domain "
            f"for clock '{selected or 'none'}'"
        )
    domain = matches[0]
    if domain.power_up is PowerUpPolicy.RESET:
        raise PhysicalDomainError(
            "direct SystemVerilog does not yet publish power_up reset contracts; "
            "a portable common initialization mechanism is not frozen"
        )
    return domain


def _domain_token(domain: ClockDomain) -> str:
    """Return a deterministic backend-private token for one typed domain."""

    payload = (
        domain.clock,
        domain.reset,
        domain.edge.value,
        domain.reset_mode.value,
        domain.reset_polarity.value,
        domain.power_up.value,
        domain.reset_release_mode.value,
        domain.reset_release_cycles,
    )
    return hashlib.sha256(repr(payload).encode()).hexdigest()[:12]


def _module_scope_identifiers(module: Module, identifier) -> set[str]:
    """Return conservative physical names already owned by one SV module.

    The reset conditioner lives in the same lexical scope as public ports and
    semantic state.  Collect every source-level name that can contribute an
    emitted declaration so backend-private names never collide with legal
    user identifiers.  Over-collection is harmless and deterministic.
    """

    names = {module.name}
    for value in (module.clock, module.reset):
        if value is not None:
            names.add(value)
    for collection in (
        module.ports,
        module.locals,
        module.registers,
        module.fifos,
        module.memories,
        module.roms,
        module.instances,
        module.request_responses,
        module.protocol_endpoints,
        module.aggregate_protocol_endpoints,
        module.csr_blocks,
        module.rules,
        module.functions,
        module.callable_definitions,
    ):
        for item in collection:
            name = getattr(item, "name", None)
            if isinstance(name, str) and name:
                names.add(name)
    return {identifier(name) for name in names}


def _reset_conditioning_names(module: Module, identifier) -> tuple[str, str]:
    """Allocate the conditioner registers and effective-reset net together."""

    domain = module_domain(module)
    token = _domain_token(domain)
    used = _module_scope_identifiers(module, identifier)
    semantic_identity = "|".join((
        "safe-reset",
        domain.clock,
        domain.reset,
        domain.edge.value,
        domain.reset_mode.value,
        domain.reset_polarity.value,
        domain.reset_release_mode.value,
        str(domain.reset_release_cycles),
    ))
    stages = allocate_private_rtl_identifier(
        f"zlang_reset_release_{token}",
        semantic_identity=f"{semantic_identity}|stages",
        used=used,
    )
    effective = allocate_private_rtl_identifier(
        f"zlang_reset_effective_{token}",
        semantic_identity=f"{semantic_identity}|effective",
        used=used,
    )
    return stages, effective


def _contains_staged_expression(value: object, seen: set[int]) -> bool:
    if isinstance(value, (Delay, Pipeline)):
        return True
    if isinstance(value, tuple):
        return any(_contains_staged_expression(item, seen) for item in value)
    if not is_dataclass(value) or isinstance(value, type):
        return False
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    return any(
        _contains_staged_expression(getattr(value, item.name), seen)
        for item in fields(value)
        if item.name not in {"type", "origin", "source_origin"}
    )


def module_requires_reset_conditioner(module: Module) -> bool:
    """Return whether emitted state consumes the conditioned root reset.

    A clocked but purely combinational module has no reset epoch to release and
    therefore must not grow an unused synchronizer.  Stateful descendants and
    buffered protocol helpers count as consumers because the root owns their
    single conditioned reset.
    """

    if any((
        module.registers,
        module.next_assignments,
        module.rules,
        module.fifos,
        module.memories,
        module.roms,
        module.csr_blocks,
        module.request_responses,
        module.request_response_connections,
        module.arbiters,
        module.elastic_pipeline_regions,
    )):
        return True
    if any(
        port.protocol in {InterfaceProtocol.CREDIT, InterfaceProtocol.VC_CREDIT}
        for port in module.ports
    ):
        return True
    if any(
        connection.buffer_depth
        or connection.adapter is not None
        or connection.crossing is not None
        for connection in module.connections
    ):
        return True
    if any(
        connection.buffer_depth
        or connection.request_buffer_depth
        or connection.response_buffer_depth
        or connection.adapter is not None
        or connection.crossing is not None
        for connection in module.hierarchical_connections
    ):
        return True
    if any(
        connection.crossing is not None
        for connection in module.aggregate_protocol_connections
    ):
        return True
    if _contains_staged_expression(module.assignments, set()):
        return True
    return any(module_requires_reset_conditioner(child) for child in module.children)


def effective_reset_signal(
    module: Module,
    identifier,
    clock: str | None = None,
) -> str:
    """Return the reset consumed by state in this physical component.

    Native reset contracts retain the exact historical port spelling.  A
    synchronized-release root instead consumes the deterministic conditioned
    signal emitted by :func:`reset_conditioner_lines`.
    """

    domain = module_domain(module, clock)
    if (
        domain.reset_release_mode is ResetReleaseMode.NATIVE
        or not module_requires_reset_conditioner(module)
    ):
        return identifier(domain.reset)
    return _reset_conditioning_names(module, identifier)[1]


def native_release_module(module: Module) -> Module:
    """Return a backend-local internal-component reset ABI.

    A composed child receives its already-conditioned reset from its parent.
    Keeping assertion mode, edge, and polarity while making release native
    prevents every reusable child specialization from creating another reset
    synchronizer.  This copy is emission-only and never changes semantic IR.
    """

    if not any(
        domain.reset_release_mode is ResetReleaseMode.SYNCHRONIZED
        for domain in module.clock_domains
    ):
        return module
    return replace(
        module,
        clock_domains=tuple(
            replace(
                domain,
                reset_release_mode=ResetReleaseMode.NATIVE,
                reset_release_cycles=0,
            )
            for domain in module.clock_domains
        ),
    )


def reset_conditioner_lines(module: Module, identifier) -> tuple[str, ...]:
    """Emit the one root-domain async-assert/sync-release conditioner."""

    synchronized = tuple(
        domain
        for domain in module.clock_domains
        if domain.reset_release_mode is ResetReleaseMode.SYNCHRONIZED
    )
    if not synchronized or not module_requires_reset_conditioner(module):
        return ()
    if len(module.clock_domains) != 1 or len(synchronized) != 1:
        raise PhysicalDomainError(
            "synchronized reset release requires exactly one physical domain"
        )
    domain = module_domain(module)
    if domain.reset_release_cycles != 2:
        raise PhysicalDomainError(
            "direct SystemVerilog synchronized reset release requires two cycles"
        )
    stages, effective = _reset_conditioning_names(module, identifier)
    clock = identifier(domain.clock)
    reset = identifier(domain.reset)
    clock_edge = "posedge" if domain.edge is ClockEdge.RISING else "negedge"
    reset_edge = (
        "posedge"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else "negedge"
    )
    asserted = (
        reset
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else f"!{reset}"
    )
    asserted_bits = (
        "2'b11"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else "2'b00"
    )
    deasserted_bit = (
        "1'b0"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else "1'b1"
    )
    return (
        f'  (* ASYNC_REG = "TRUE" *) logic [1:0] {stages};',
        f"  logic {effective};",
        f"  always_ff @({clock_edge} {clock} or {reset_edge} {reset}) begin",
        f"    if ({asserted}) {stages} <= {asserted_bits};",
        f"    else {stages} <= {{{stages}[0], {deasserted_bit}}};",
        "  end",
        f"  assign {effective} = {stages}[1];",
    )


def clock_event(module: Module, identifier, clock: str | None = None) -> str:
    domain = module_domain(module, clock)
    edge = "posedge" if domain.edge is ClockEdge.RISING else "negedge"
    event = f"{edge} {identifier(domain.clock)}"
    if domain.reset_mode is ResetMode.ASYNCHRONOUS:
        reset_edge = (
            "posedge"
            if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
            else "negedge"
        )
        event += f" or {reset_edge} {effective_reset_signal(module, identifier, clock)}"
    return event


def reset_asserted(module: Module, identifier, clock: str | None = None) -> str:
    domain = module_domain(module, clock)
    reset = effective_reset_signal(module, identifier, clock)
    return (
        reset
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else f"!{reset}"
    )


def reset_deasserted(module: Module, identifier, clock: str | None = None) -> str:
    domain = module_domain(module, clock)
    reset = effective_reset_signal(module, identifier, clock)
    return (
        f"!{reset}"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else reset
    )
