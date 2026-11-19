# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned hierarchy validation services.

Hierarchy structure and current-cycle child dependencies are validated here;
semantic orchestration supplies already typed modules and bindings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import NamedTuple

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import csr as ir_csr
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import state as ir_state
from zlang.ir import storage as ir_storage
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir.interfaces import (
    InterfaceProtocol,
    ReadyValidSignal,
    parse_ready_valid_field_name,
    parse_request_response_field_name,
    ready_valid_field_name,
    request_response_field_name,
    request_response_signal_is_input,
    RequestResponseChannel,
)
from zlang.ir.traversal import expression_children
from zlang.ir.static_projection import StaticView, project_static_bits, static_parts, static_view
from zlang.ir.types import HardwareType, UIntType
from zlang.source import SourceOrigin

from .connection_validation import hierarchy_connection_drivers
from .csr.bindings import csr_output_controls
from .errors import SemanticError
from .module_validation import reject_dependency_cycles
from .storage_validation import fifo_observation_dependencies, memory_read_dependencies


@dataclass(frozen=True)
class StateDomainResolver:
    """Resolve state ownership against one prepared module domain set."""

    clock_domains: tuple[ir_cdc.ClockDomain, ...]

    def resolve(
        self,
        kind: str,
        name: str,
        requested: str | None,
        inferred: Iterable[str | None] = (),
        *,
        origin: SourceOrigin | None = None,
    ) -> str:
        if not self.clock_domains:
            raise SemanticError(
                f"{kind} '{name}' requires a clock and reset",
                primary=origin,
                notes=(
                    "A child module can inherit its enclosing clock/reset contract; "
                    "checking it as a standalone top has no enclosing contract.",
                ),
                fixes=(
                    "instantiate this module beneath an explicit clock/reset owner, "
                    "or supply an explicit root clock/reset contract",
                ),
            )
        domains_by_clock = {
            domain.clock: domain for domain in self.clock_domains
        }
        if requested is not None:
            if requested not in domains_by_clock:
                raise SemanticError(
                    f"{kind} '{name}' references unknown clock domain "
                    f"'{requested}'",
                    code="ZL-DOMAIN-UNKNOWN",
                    primary=origin,
                )
            return requested
        candidates = {item for item in inferred if item is not None}
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            rendered = ", ".join(sorted(candidates))
            raise SemanticError(
                f"clock-domain mismatch while inferring {kind} '{name}': "
                f"dynamic values belong to {rendered}",
                code="ZL-DOMAIN-CROSSING",
                primary=origin,
                fixes=("insert an explicit supported clock-domain crossing",),
            )
        if len(self.clock_domains) == 1:
            return self.clock_domains[0].clock
        available = ", ".join(
            domain.clock for domain in self.clock_domains
        )
        raise SemanticError(
            f"ambiguous clock domain for {kind} '{name}'; available domains: "
            f"{available}",
            code="ZL-DOMAIN-AMBIGUOUS",
            primary=origin,
            fixes=(
                f"annotate the declaration with @{self.clock_domains[0].clock} "
                "or another listed domain",
            ),
        )


def signature_error(
    message: str,
    *,
    code: str = "ZL-INTERFACE-CONFORMANCE",
) -> None:
    raise SemanticError(
        message,
        code=code,
        fixes=(
            "make the module public signature exactly match the named interface",
        ),
    )


def clock_domain_from_source(
    clock: ast.ClockPhysicalDecl,
    reset: ast.ResetPhysicalDecl,
    *,
    source_unit: str | None = None,
    source_digest: str | None = None,
) -> ir_cdc.ClockDomain:
    origin_span = reset.origin or clock.origin
    origin = (
        None
        if origin_span is None
        else SourceOrigin(
            origin_span,
            f"clock/reset domain {clock.name}",
            source_unit,
            source_digest,
        )
    )
    try:
        return ir_cdc.ClockDomain(
            clock.name,
            reset.name,
            ir_cdc.ClockEdge(clock.edge),
            ir_cdc.ResetMode(reset.mode),
            ir_cdc.ResetPolarity(reset.polarity),
            ir_cdc.PowerUpPolicy(reset.power_up),
            origin,
            ir_cdc.ResetReleaseMode(reset.release_mode),
            reset.release_cycles,
        )
    except ValueError as error:
        raise SemanticError(
            f"invalid reset contract for '{reset.name}': {error}"
        ) from error


def validate_async_reset_domain_scope(
    clock_domains: tuple[ir_cdc.ClockDomain, ...],
) -> None:
    """Validate the complete table; async release is owned per domain."""

    for domain in clock_domains:
        domain.validate()


@dataclass(frozen=True)
class ClockDomainAnalysisProduct:
    """Resolved clock/reset ownership for one module body."""

    domains: tuple[ir_cdc.ClockDomain, ...]
    default_clock: str | None
    default_reset: str | None
    timing_names: frozenset[str]


class ClockDomainAnalyzer:
    """Own physical clock/reset validation and inherited-domain selection."""

    def analyze(
        self,
        module: ast.Module,
        inherited: tuple[str, str] | ir_cdc.ClockDomain | None,
        *,
        source_unit: str | None = None,
        source_digest: str | None = None,
    ) -> ClockDomainAnalysisProduct:
        clock_origins = {
            item.name: SourceOrigin(item.origin, f"clock {item.name}", source_unit, source_digest)
            for item in module.clock_physical if item.origin is not None
        }
        reset_origins = {
            item.name: SourceOrigin(item.origin, f"reset {item.name}", source_unit, source_digest)
            for item in module.reset_physical if item.origin is not None
        }
        if len(set(module.clocks)) != len(module.clocks):
            raise SemanticError(
                "duplicate clock declaration",
                primary=clock_origins.get(
                    next(name for i, name in enumerate(module.clocks) if name in module.clocks[:i])
                ),
            )
        resets = module.reset_domains or tuple(
            (name, None) for name in module.resets
        )
        if len({name for name, _ in resets}) != len(resets):
            raise SemanticError(
                "duplicate reset declaration",
                primary=reset_origins.get(
                    next(name for i, name in enumerate(module.resets) if name in module.resets[:i])
                ),
            )
        if bool(module.clocks) != bool(resets):
            raise SemanticError(
                "clock and reset must be declared together",
                primary=next(
                    iter(clock_origins.values()), next(iter(reset_origins.values()), None)
                ),
            )
        clocks = module.clock_physical or tuple(
            ast.ClockPhysicalDecl(name) for name in module.clocks
        )
        reset_specs = module.reset_physical or tuple(
            ast.ResetPhysicalDecl(name, domain) for name, domain in resets
        )
        if tuple(item.name for item in clocks) != module.clocks:
            raise SemanticError(
                "physical clock declarations must match clock declarations",
                primary=next(iter(clock_origins.values()), None),
            )
        if tuple(item.name for item in reset_specs) != module.resets:
            raise SemanticError(
                "physical reset declarations must match reset declarations",
                primary=next(iter(reset_origins.values()), None),
            )
        clocks_by_name = {item.name: item for item in clocks}
        resets_by_name = {item.name: item for item in reset_specs}

        if not module.clocks and inherited is not None:
            domain = (
                inherited
                if isinstance(inherited, ir_cdc.ClockDomain)
                else ir_cdc.ClockDomain(*inherited)
            )
            domains = (domain,)
            default_clock, default_reset = domain.clock, domain.reset
        elif not module.clocks:
            domains = ()
            default_clock = default_reset = None
        elif len(module.clocks) == 1:
            if len(resets) != 1:
                raise SemanticError(
                    "a single-clock module requires exactly one reset",
                    primary=reset_origins.get(resets[-1][0])
                    if resets
                    else clock_origins.get(module.clocks[0]),
                )
            default_clock = module.clocks[0]
            default_reset, reset_domain = resets[0]
            if reset_domain is not None and reset_domain != default_clock:
                raise SemanticError(
                    f"reset '{default_reset}' references unknown clock domain "
                    f"'{reset_domain}'",
                    primary=reset_origins.get(default_reset),
                )
            domains = (clock_domain_from_source(
                clocks_by_name[default_clock],
                resets_by_name[default_reset],
                source_unit=source_unit,
                source_digest=source_digest,
            ),)
        else:
            default_clock = default_reset = None
            by_clock: dict[str, str] = {}
            for reset_name, reset_domain in resets:
                if reset_domain is None:
                    raise SemanticError(
                        f"reset '{reset_name}' requires an explicit clock domain",
                        primary=reset_origins.get(reset_name),
                    )
                if reset_domain not in module.clocks:
                    raise SemanticError(
                        f"reset '{reset_name}' references unknown clock domain "
                        f"'{reset_domain}'",
                        primary=reset_origins.get(reset_name),
                    )
                if reset_domain in by_clock:
                    raise SemanticError(
                        f"clock domain '{reset_domain}' has more than one reset",
                        primary=reset_origins.get(reset_name),
                    )
                by_clock[reset_domain] = reset_name
            missing = set(module.clocks) - by_clock.keys()
            if missing:
                raise SemanticError(
                    f"clock domain '{sorted(missing)[0]}' has no reset",
                    primary=clock_origins.get(sorted(missing)[0]),
                )
            domains = tuple(
                clock_domain_from_source(
                    clocks_by_name[name],
                    resets_by_name[by_clock[name]],
                    source_unit=source_unit,
                    source_digest=source_digest,
                )
                for name in module.clocks
            )
        validate_async_reset_domain_scope(domains)
        if any(domain.clock == domain.reset for domain in domains):
            raise SemanticError(
                "clock and reset must have different names",
                primary=reset_origins.get(
                    next(domain.reset for domain in domains if domain.clock == domain.reset)
                ),
            )
        return ClockDomainAnalysisProduct(
            domains,
            default_clock,
            default_reset,
            frozenset(
                name for domain in domains for name in (domain.clock, domain.reset)
            ),
        )


def interface_clock_domains(
    declaration: ast.ModuleInterfaceDecl,
) -> tuple[ir_cdc.ClockDomain, ...]:
    clocks = declaration.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in declaration.clocks
    )
    resets = declaration.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain)
        for name, domain in (
            declaration.reset_domains
            or tuple((name, None) for name in declaration.resets)
        )
    )
    if len(set(declaration.clocks)) != len(declaration.clocks):
        signature_error(
            f"module interface '{declaration.name}' has duplicate clock declarations"
        )
    bindings = declaration.reset_domains or tuple(
        (name, None) for name in declaration.resets
    )
    if len({name for name, _ in bindings}) != len(bindings):
        signature_error(
            f"module interface '{declaration.name}' has duplicate reset declarations"
        )
    if bool(declaration.clocks) != bool(bindings):
        signature_error(
            f"module interface '{declaration.name}' must declare clock and reset together"
        )
    if not declaration.clocks:
        return ()
    if len(declaration.clocks) == 1:
        if len(bindings) != 1:
            signature_error(
                f"module interface '{declaration.name}' requires exactly one reset"
            )
        reset, reset_domain = bindings[0]
        clock = declaration.clocks[0]
        if reset_domain is not None and reset_domain != clock:
            signature_error(
                f"module interface reset '{reset}' references unknown domain "
                f"'{reset_domain}'"
            )
        if reset == clock:
            signature_error("module interface clock and reset names must differ")
        clock_decl = next(item for item in clocks if item.name == clock)
        reset_decl = next(item for item in resets if item.name == reset)
        return (clock_domain_from_source(clock_decl, reset_decl),)
    by_clock: dict[str, str] = {}
    for reset, domain in bindings:
        if domain is None or domain not in declaration.clocks:
            signature_error(
                f"module interface reset '{reset}' requires a declared clock domain"
            )
        if domain in by_clock:
            signature_error(
                f"module interface clock domain '{domain}' has multiple resets"
            )
        by_clock[domain] = reset
    missing = set(declaration.clocks) - by_clock.keys()
    if missing:
        signature_error(
            f"module interface clock domain '{sorted(missing)[0]}' has no reset"
        )
    domains = tuple(
        clock_domain_from_source(
            next(item for item in clocks if item.name == clock),
            next(item for item in resets if item.name == by_clock[clock]),
        )
        for clock in declaration.clocks
    )
    validate_async_reset_domain_scope(domains)
    return domains


class HierarchyAnalyzer:
    """Own bounded nested instance-array hierarchy policy."""

    def validate_nested_instance_array_child(
        self,
        array_name: str,
        child: ir_module.Module,
        *,
        hierarchy_cache: ir_hierarchy.HierarchyTraversalCache | None = None,
    ) -> None:
        """Validate the bounded nested hierarchy admitted below an array element.

        Compile-time arrays are structural applications of an already typed child
        specialization.  A nested hierarchy is therefore safe only when every
        transitive component stays inside the existing closed scalar/ready-valid
        ABI: one synchronous domain, direct connections, and no protocol or
        storage semantics which would require a new parent-level scheduler.  The
        hierarchy index supplies the authoritative physical paths; no backend is
        allowed to reconstruct them from generated names.
        """

        try:
            hierarchy = ir_hierarchy.build_hierarchy_index(child, cache=hierarchy_cache)
        except ir_hierarchy.HierarchyError as error:
            raise SemanticError(
                f"instance array '{array_name}' has invalid nested hierarchy: {error}"
            ) from error

        root_domain = (child.clock, child.reset)
        for entry in hierarchy.entries:
            current = entry.module
            rendered_path = ".".join(entry.physical_path)
            if current.is_multi_clock:
                raise SemanticError(
                    f"instance array '{array_name}' nested child '{rendered_path}' "
                    "must share exactly one synchronous clock/reset domain; CDC "
                    "children are not supported"
                )
            if current.is_sequential and (current.clock, current.reset) != root_domain:
                raise SemanticError(
                    f"instance array '{array_name}' nested child '{rendered_path}' "
                    "does not use the array element's clock/reset domain"
                )
            if current.request_responses or current.request_response_connections:
                raise SemanticError(
                    f"instance array '{array_name}' nested child '{rendered_path}' "
                    "does not support request/response hierarchy"
                )
            if (
                current.aggregate_protocol_endpoints
                or current.aggregate_protocol_connections
            ):
                raise SemanticError(
                    f"instance array '{array_name}' nested child '{rendered_path}' "
                    "does not support aggregate protocol hierarchy"
                )
            if current.csr_blocks:
                raise SemanticError(
                    f"instance array '{array_name}' nested child '{rendered_path}' "
                    "does not support CSR state"
                )
            if current.memories or current.roms or current.fifos:
                raise SemanticError(
                    f"instance array '{array_name}' nested child '{rendered_path}' "
                    "does not support transitive storage resources"
                )
            unsupported_ports = tuple(
                port
                for port in current.ports
                if port.protocol not in {
                    InterfaceProtocol.WIRE,
                    InterfaceProtocol.READY_VALID,
                }
            )
            if unsupported_ports:
                raise SemanticError(
                    f"instance array '{array_name}' nested child '{rendered_path}' "
                    "supports only scalar wire and direct ready/valid ports"
                )
            for connection in current.connections:
                if (
                    connection.buffer_depth
                    or connection.adapter is not None
                    or connection.crossing is not None
                ):
                    raise SemanticError(
                        f"instance array '{array_name}' nested child "
                        f"'{rendered_path}' requires a direct same-domain "
                        "connection without buffering, adapters, or crossings"
                    )
            for connection in current.hierarchical_connections:
                if (
                    connection.buffer_depth
                    or connection.request_buffer_depth
                    or connection.response_buffer_depth
                    or connection.adapter is not None
                    or connection.crossing is not None
                ):
                    raise SemanticError(
                        f"instance array '{array_name}' nested child "
                        f"'{rendered_path}' requires a direct same-domain "
                        "connection without buffering, adapters, or crossings"
                    )


class _DependencyNode(NamedTuple):
    kind: str
    name: str
    field: str
    read_bits: tuple[int, ...] = ()


class _InputDependency(NamedTuple):
    name: str
    bits: tuple[int, ...] = ()


def _project_dependency(
    expression: ir_expr.Expression,
    bits: tuple[int, ...],
    locals_: dict[str, ir_expr.Expression],
) -> ir_expr.Expression:
    if not bits:
        return expression
    msb, lsb = bits
    return project_static_bits(StaticView(expression, msb, lsb), locals_, UIntType(msb - lsb + 1))


def _storage_dependency_controls(
    memories: tuple[ir_storage.Memory, ...],
    fifos: tuple[ir_storage.Fifo, ...],
) -> dict[_DependencyNode, tuple[ir_expr.Expression, ...]]:
    return {
        **{
            _DependencyNode("memory", memory.name, port): controls
            for memory in memories
            for port, controls in memory_read_dependencies(memory).items()
        },
        **{
            _DependencyNode("fifo", fifo.name, signal.value): controls
            for fifo in fifos
            for signal, controls in fifo_observation_dependencies(fifo).items()
        },
    }


def _protocol_assignment_drivers(
    assignments: Iterable[ir_module.Assignment],
) -> dict[str, tuple[ir_expr.Expression, ...]]:
    drivers = {}
    for assignment in assignments:
        if not isinstance(assignment.signal, ReadyValidSignal):
            continue
        target = assignment.target
        if (
            isinstance(target, ir_module.Port)
            and target.protocol is InterfaceProtocol.READY_VALID
            and assignment.channel is None
        ):
            leaf = ready_valid_field_name(target.name, assignment.signal)
        elif (
            isinstance(target, ir_module.RequestResponseInterface)
            and assignment.channel is not None
        ):
            leaf = request_response_field_name(target.name, assignment.channel, assignment.signal)
        else:
            continue
        drivers[leaf] = (assignment.expression,)
    return drivers


class HierarchyDependencyValidator:
    """Reject current-cycle feedback through typed child and storage outputs."""

    def validate(
        self,
        child_irs: dict[str, ir_module.Module],
        bindings: tuple[ir_module.InstancePortBinding, ...],
        locals_: tuple[ir_module.LocalValue, ...],
        memories: tuple[ir_storage.Memory, ...] = (),
        fifos: tuple[ir_storage.Fifo, ...] = (),
        instance_origins: dict[str, SourceOrigin | None] | None = None,
        module_name: str = "",
        connections: tuple[ir_module.Connection, ...] = (),
        hierarchical_connections: tuple[ir_module.HierarchicalConnection, ...] = (),
        request_response_connections: tuple[ir_module.RequestResponseConnection, ...] = (),
        assignments: tuple[ir_module.Assignment, ...] = (),
        csr_output_paths: dict[
            tuple[str, ...], tuple[str, HardwareType, str | None]
        ] | None = None,
    ) -> None:
        """Follow typed child bindings and current-cycle storage observations."""

        local_values = {item.name: item.expression for item in locals_}
        parent_storage = _storage_dependency_controls(memories, fifos)
        parent_signals, connected_inputs = hierarchy_connection_drivers(
            module_name, connections, hierarchical_connections, request_response_connections,
        )
        parent_signals.update(_protocol_assignment_drivers(assignments))
        bindings = (*bindings, *connected_inputs)
        routed_inputs = {(item.instance, item.port) for item in connected_inputs}

        def references(
            expression: ir_expr.Expression,
            *,
            local_expressions: dict[str, ir_expr.Expression],
            storage_controls: dict[_DependencyNode, tuple[ir_expr.Expression, ...]],
            known_inputs: set[str] | None = None,
            signal_sources: dict[str, tuple[ir_expr.Expression, ...]] | None = None,
            instance_inputs: Callable[
                [_DependencyNode], Iterable[ir_expr.Expression]
            ] | None = None,
            active_locals: frozenset[str] = frozenset(),
        ) -> tuple[set[_InputDependency], dict[_DependencyNode, SourceOrigin | None]]:
            input_names: set[_InputDependency] = set()
            local_signals = signal_sources or {}
            outputs: dict[_DependencyNode, SourceOrigin | None] = {}
            def visit(
                value: object,
                active: frozenset[str],
                active_storage: frozenset[_DependencyNode] = frozenset(),
            ) -> None:
                selected = static_view(value)
                if selected is not None:
                    for item, msb, lsb in static_parts(selected, local_expressions, active):
                        bits = () if lsb == 0 and msb == item.type.width - 1 else (msb, lsb)
                        if isinstance(item, ir_expr.InstanceOutputRef):
                            visit_child_output(item, bits, active, active_storage)
                        elif isinstance(item, ir_expr.InputRef):
                            visit_name(item.name, active, active_storage, bits=bits)
                        elif isinstance(item, (ir_expr.ReadyValidRef, ir_expr.RequestResponseRef)):
                            visit_protocol(item, active, active_storage, bits=bits)
                        else:
                            visit(item, active, active_storage)
                    return
                if isinstance(value, ir_expr.InputRef):
                    visit_name(value.name, active, active_storage)
                    return
                if isinstance(value, (ir_expr.ReadyValidRef, ir_expr.RequestResponseRef)):
                    visit_protocol(value, active, active_storage)
                    return
                if isinstance(value, ir_expr.InstanceOutputRef):
                    visit_child_output(value, (), active, active_storage)
                    return
                if isinstance(value, (ir_expr.MemoryRef, ir_expr.FifoRef)):
                    if isinstance(value, ir_expr.MemoryRef):
                        if value.signal is not ir_storage.MemorySignal.READ_DATA:
                            return
                        node = _DependencyNode("memory", value.memory, value.port or "")
                    else:
                        node = _DependencyNode("fifo", value.fifo, value.signal.value)
                    if node in storage_controls:
                        if instance_inputs is None:
                            outputs[node] = value.origin
                        elif node not in active_storage:
                            for control in storage_controls[node]:
                                visit(control, active, active_storage | {node})
                    return
                if isinstance(
                    value,
                    (
                        ir_expr.RegisterRef,
                        ir_expr.RomRef,
                        ir_expr.Delay,
                        ir_expr.Pipeline,
                        ir_expr.Constant,
                        ir_expr.ParameterRef,
                        ir_expr.FunctionalCaptureRef,
                        ir_expr.FunctionalValue,
                        ir_expr.FunctionalTableLookup,
                    ),
                ):
                    return
                if isinstance(value, ir_expr.Expression):
                    for child in expression_children(value):
                        visit(
                            child,
                            active,
                            active_storage,
                        )

            def visit_name(
                name: str,
                active: frozenset[str],
                active_storage: frozenset[_DependencyNode],
                *,
                bits: tuple[int, ...] = (),
            ) -> None:
                replacement = local_expressions.get(name)
                controls = (
                    (replacement,) if replacement is not None
                    else local_signals.get(name, ())
                )
                if controls:
                    if name in active:
                        raise SemanticError(
                            f"cyclic local dependency '{name}' in child dependency analysis"
                        )
                    for control in controls:
                        visit(_project_dependency(control, bits, {}), active | {name}, active_storage)
                elif known_inputs is None or name in known_inputs:
                    input_names.add(_InputDependency(name, bits))

            def visit_protocol(
                value: ir_expr.ReadyValidRef | ir_expr.RequestResponseRef,
                active: frozenset[str],
                active_storage: frozenset[_DependencyNode],
                *,
                bits: tuple[int, ...] = (),
            ) -> None:
                signals = (
                    (ReadyValidSignal.VALID, ReadyValidSignal.READY)
                    if value.signal is ReadyValidSignal.TRANSFER else (value.signal,)
                )
                for signal in signals:
                    name = (
                        request_response_field_name(value.interface, value.channel, signal)
                        if isinstance(value, ir_expr.RequestResponseRef)
                        else ready_valid_field_name(value.interface, signal)
                    )
                    visit_name(name, active, active_storage, bits=bits)

            def visit_child_output(
                reference: ir_expr.InstanceOutputRef,
                bits: tuple[int, ...],
                active: frozenset[str],
                active_storage: frozenset[_DependencyNode],
            ) -> None:
                node = _DependencyNode("child", reference.instance, reference.port, bits)
                if instance_inputs is None:
                    outputs[node] = reference.origin
                else:
                    for binding in instance_inputs(node):
                        visit(binding, active, active_storage)

            visit(expression, active_locals)
            return input_names, outputs

        output_summary_cache: dict[
            tuple[int, tuple[tuple[str, int], ...], str, tuple[int, ...]],
            dict[str, frozenset[_InputDependency]],
        ] = {}
        active_summary_modules: set[int] = set()

        def output_input_dependencies(
            current: ir_module.Module,
            access_constants: dict[str, int],
            observed_output: str = "",
            read_bits: tuple[int, ...] = (),
        ) -> dict[str, frozenset[_InputDependency]]:
            """Summarize each observed output in terms of its physical input leaves.

            A direct child output is not itself an input dependency.  Resolve it
            through that child's already typed output summary and exact scalar
            bindings.  This makes the summary transitive across an arbitrary
            bounded hierarchy while registers and latency-one storage remain
            explicit current-cycle cuts.
            """

            module_key = id(current)
            key = (module_key, tuple(sorted(access_constants.items())), observed_output, read_bits)
            cached = output_summary_cache.get(key)
            if cached is not None:
                return cached
            if module_key in active_summary_modules:
                raise SemanticError(
                    f"cyclic typed child hierarchy while summarizing '{current.name}'"
                )
            active_summary_modules.add(module_key)
            try:
                current_inputs = {
                    port.name for port in current.inputs if port.protocol is InterfaceProtocol.WIRE
                }
                for port in current.ports:
                    if port.protocol is not InterfaceProtocol.READY_VALID:
                        continue
                    signals = (
                        (ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID)
                        if port.direction is ir_module.PortDirection.INPUT
                        else (ReadyValidSignal.READY,)
                    )
                    current_inputs.update(
                        ready_valid_field_name(port.name, signal) for signal in signals
                    )
                current_inputs.update(
                    request_response_field_name(interface.name, channel, signal)
                    for interface in current.request_responses
                    for channel in RequestResponseChannel
                    for signal in (ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID, ReadyValidSignal.READY)
                    if request_response_signal_is_input(interface.role, channel, signal)
                )
                read_data = current.csr_access.read_data_port if current.csr_access else None
                csr_bits = read_bits if observed_output == read_data else ()
                signal_sources = csr_output_controls(current, access_constants, csr_bits)
                connected_signals, connected_inputs = hierarchy_connection_drivers(
                    current.name, current.connections, current.hierarchical_connections,
                    current.request_response_connections,
                )
                signal_sources.update(connected_signals)
                signal_sources.update(_protocol_assignment_drivers(current.assignments))
                current_locals = {
                    item.name: item.expression for item in current.locals
                }
                current_storage = _storage_dependency_controls(
                    current.memories, current.fifos,
                )
                current_children = {
                    elaborated.instance.name: child
                    for child, elaborated in zip(
                        current.children,
                        current.elaborated_instances,
                        strict=True,
                    )
                }
                current_bindings = {
                    (item.instance, item.port): item.expression
                    for item in (*current.instance_bindings, *connected_inputs)
                }

                nested_constants: dict[str, dict[str, int]] = {}

                def nested_instance_inputs(
                    reference: _DependencyNode,
                ) -> Iterable[ir_expr.Expression]:
                    nested = current_children.get(reference.name)
                    if nested is None:
                        return ()
                    if reference.name not in nested_constants:
                        nested_constants[reference.name] = ir_hierarchy.csr_access_constants(
                            nested, reference.name, current_bindings, current_locals, access_constants,
                        )
                    nested_summary = output_input_dependencies(
                        nested, nested_constants[reference.name],
                        reference.field if reference.read_bits else "", reference.read_bits,
                    )
                    return tuple(
                        _project_dependency(binding, nested_input.bits, current_locals)
                        for nested_input in nested_summary.get(reference.field, ())
                        if (binding := current_bindings.get((reference.name, nested_input.name))) is not None
                    )

                def summarize_expression(
                    expression: ir_expr.Expression,
                ) -> set[_InputDependency]:
                    dependencies, _ = references(
                        expression,
                        local_expressions=current_locals,
                        storage_controls=current_storage,
                        known_inputs=current_inputs,
                        signal_sources=signal_sources,
                        instance_inputs=nested_instance_inputs,
                    )
                    return dependencies

                if csr_bits:
                    controls = signal_sources.get(observed_output, ())
                    summary = {
                        observed_output: frozenset().union(*(summarize_expression(control) for control in controls))
                    }
                    output_summary_cache[key] = summary
                    return summary

                scheduled_guard_cache: dict[
                    str, tuple[ir_expr.Expression, ...]
                ] = {}
                scheduled_regions_cache = None

                def scheduled_guard_dependencies(
                    rule_name: str,
                ) -> tuple[ir_expr.Expression, ...]:
                    nonlocal scheduled_regions_cache
                    cached_guards = scheduled_guard_cache.get(rule_name)
                    if cached_guards is not None:
                        return cached_guards
                    transition = current.resolved_transition
                    if transition is None:
                        rule = next(
                            item for item in current.rules
                            if item.name == rule_name
                        )
                        result = (
                            rule.guard,
                            *(
                                action.activation
                                for action in rule.actions
                                if action.activation is not None
                            ),
                        )
                    else:
                        groups = ir_state.ordered_groups(transition)
                        activation_predicates = (
                            ir_state.conditional_activation_predicates(transition)
                        )
                        fifo_dimensions = sum(
                            resource.kind is ir_state.StateResourceKind.FIFO
                            for resource in transition.resources
                        )
                        if scheduled_regions_cache is None:
                            scheduled_regions_cache = (
                                ir_state.selection_regions_for_transition(transition)
                            )
                        regions = scheduled_regions_cache[rule_name]
                        guards = tuple(
                            group.guard
                            for index, group in enumerate(groups)
                            if any(
                                region[fifo_dimensions + index] is not None
                                for region in regions
                            )
                        )
                        activation_offset = fifo_dimensions + len(groups)
                        activations = tuple(
                            activation
                            for index, activation in enumerate(activation_predicates)
                            if any(
                                region[activation_offset + index] is not None
                                for region in regions
                            )
                        )
                        result = (*guards, *activations)
                    scheduled_guard_cache[rule_name] = result
                    return result

                output_names = {port.name for port in current.outputs}
                summary: dict[str, frozenset[_InputDependency]] = {
                    name: frozenset().union(*(
                        summarize_expression(_project_dependency(control, read_bits, current_locals))
                        for control in controls
                    ))
                    for name, controls in signal_sources.items()
                    if (not observed_output or name == observed_output)
                    and (name in output_names
                         or parse_ready_valid_field_name(name) is not None
                         or parse_request_response_field_name(name) is not None)
                }
                for output in current.outputs:
                    if observed_output and output.name != observed_output:
                        continue
                    if output.protocol is not InterfaceProtocol.WIRE or output.name in summary:
                        continue
                    if output.registered:
                        # A public register exposes pre-edge state. Its rule
                        # guards and operands affect the next state only.
                        summary[output.name] = frozenset()
                        continue
                    dependencies: set[_InputDependency] = set()
                    dependencies.update(*(
                        summarize_expression(_project_dependency(
                            assignment.expression, read_bits, current_locals,
                        ))
                        for assignment in current.assignments
                        if assignment.target.name == output.name
                        and assignment.signal is None
                        and assignment.channel is None
                    ))
                    for rule in current.rules:
                        for action in rule.actions:
                            if action.target.name != output.name:
                                continue
                            for guard in scheduled_guard_dependencies(rule.name):
                                dependencies.update(summarize_expression(guard))
                            # Scheduler selection may be independent of this exact
                            # effect predicate when the same group also contains an
                            # unconditional register/storage effect.  The output
                            # value nevertheless still depends on its own branch
                            # activation and must expose that dependency to the
                            # hierarchical combinational-cycle check.
                            if action.activation is not None:
                                dependencies.update(
                                    summarize_expression(action.activation)
                                )
                            dependencies.update(
                                summarize_expression(action.expression)
                            )
                    summary[output.name] = frozenset(dependencies)
                output_summary_cache[key] = summary
                return summary
            finally:
                active_summary_modules.remove(module_key)

        binding_by_input = {
            (item.instance, item.port): item.expression for item in bindings
        }
        graph: dict[_DependencyNode, set[_DependencyNode]] = {}
        dependency_origins: dict[
            tuple[_DependencyNode, _DependencyNode], SourceOrigin | None
        ] = {}

        csr_outputs_by_instance = {
            instance: ir_csr.csr_projection_port_names(child.csr_blocks).union(
                name for name, _ in child.csr_access.output_types
            )
            for instance, child in child_irs.items()
            if child.csr_access is not None
        }
        demanded_outputs: set[_DependencyNode] = set()

        def is_csr_output(node: _DependencyNode) -> bool:
            return (
                node.kind == "child"
                and node.field in csr_outputs_by_instance.get(node.name, ())
            )

        def connect_controls(
            node: _DependencyNode,
            controls: Iterable[ir_expr.Expression],
        ) -> None:
            edges = graph.setdefault(node, set())
            for control in controls:
                _, dependencies = references(
                    control,
                    local_expressions=local_values,
                    storage_controls=parent_storage,
                    signal_sources=parent_signals,
                )
                edges.update(dependencies)
                demanded_outputs.update(
                    target for target in dependencies
                    if target.kind == "child" and (target.read_bits or is_csr_output(target))
                )
                for target, origin in dependencies.items():
                    dependency_origins[(node, target)] = control.origin or origin

        for node, controls in parent_storage.items():
            connect_controls(node, controls)

        def connect_child_output(
            output: _DependencyNode, inputs: frozenset[_InputDependency],
        ) -> None:
            edges = graph.setdefault(output, set())
            for input_name, bits in sorted(inputs):
                bound = binding_by_input.get((output.name, input_name))
                if bound is None:
                    continue
                input_node = _DependencyNode("input", output.name, input_name, bits)
                if input_node not in graph:
                    connect_controls(input_node, (_project_dependency(bound, bits, local_values),))
                for target in graph[input_node]:
                    # Keep the established direct path for child-only cycles.
                    # A storage edge retains the input port that closes the path.
                    edge = (
                        target if target.kind == "child" and (output.name, input_name) not in routed_inputs
                        else input_node
                    )
                    edges.add(edge)
                    dependency_origins[(output, edge)] = bound.origin

        bound_instances = {item.instance for item in bindings}
        constants_by_child: dict[str, dict[str, int]] = {}
        for instance, child in child_irs.items():
            # A declaration alias is not a physical instance unless it owns bindings.
            if instance not in bound_instances:
                continue
            constants = ir_hierarchy.csr_access_constants(child, instance, binding_by_input, local_values, {})
            constants_by_child[instance] = constants
            for name, input_names in output_input_dependencies(child, constants).items():
                output = _DependencyNode("child", instance, name)
                if not is_csr_output(output):
                    connect_child_output(output, input_names)

        # Follow observed child ranges and CSR leaves on demand.
        while demanded_outputs:
            pending = sorted(demanded_outputs)
            demanded_outputs.clear()
            for output in pending:
                if output in graph:
                    continue
                child = child_irs[output.name]
                constants = constants_by_child.get(output.name, {})
                sources = output_input_dependencies(
                    child, constants, output.field if output.read_bits else "", output.read_bits,
                )
                connect_child_output(output, sources.get(output.field, frozenset()))

        def render_node(node: _DependencyNode) -> str:
            if node.kind == "memory":
                path = f"{node.name}.{node.field}" if node.field else node.name
                return f"{path}.read_data"
            rr_signal = parse_request_response_field_name(node.field)
            if rr_signal is not None:
                endpoint, channel, leaf = rr_signal
                return f"{node.name}.{endpoint}.{channel.value}.{leaf.value}"
            signal = parse_ready_valid_field_name(node.field)
            endpoint = signal[0] if signal is not None else node.field
            suffix = f".{signal[1].value}" if signal is not None else ""
            child = child_irs.get(node.name)
            if child is not None:
                for aggregate in child.aggregate_protocol_endpoints:
                    for member in aggregate.members:
                        if endpoint == f"{aggregate.name}__{member.name}":
                            return f"{node.name}.{aggregate.name}.{member.name}{suffix}"
            if signal is not None:
                return f"{node.name}.{endpoint}{suffix}"
            if node.kind == "child" and csr_output_paths:
                paths = [
                    path for path, (port, _, _) in csr_output_paths.items()
                    if path[0] == node.name and port == node.field
                ]
                if paths:
                    path = min(paths, key=lambda parts: (parts[2] != "status", len(parts), parts))
                    return ".".join(path)
            return f"{node.name}.{node.field}"

        def describe_cycle(nodes: tuple[_DependencyNode, ...]) -> str:
            kinds = {node.kind for node in nodes}
            if len(kinds) == 1:
                return "FIFO" if "fifo" in kinds else nodes[0].kind
            if kinds <= {"child", "input"}:
                return "child"
            return "storage"

        def cycle_origin(nodes: tuple[_DependencyNode, ...]) -> SourceOrigin | None:
            source, target = nodes[-2:]
            if source.kind not in {"memory", "fifo"} and any(node.read_bits for node in nodes):
                # A selected range can change which edge closes the DFS.
                # Highlight the controlling storage expression when available.
                for left, right in zip(nodes, nodes[1:]):
                    if left.kind in {"memory", "fifo"}:
                        location = dependency_origins.get((left, right))
                        if location is not None:
                            return location
            return dependency_origins.get((source, target)) or (instance_origins or {}).get(source.name)

        reject_dependency_cycles(
            graph,
            render_node=render_node,
            description=describe_cycle,
            origin=cycle_origin,
        )
