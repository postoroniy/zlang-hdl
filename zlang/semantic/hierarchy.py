# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned hierarchy validation services.

Hierarchy structure and current-cycle child dependencies are validated here;
semantic orchestration supplies already typed modules and bindings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import state as ir_state
from zlang.ir import storage as ir_storage
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.traversal import expression_children
from zlang.source import SourceOrigin

from .errors import SemanticError
from .module_validation import reject_dependency_cycles


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
    ) -> str:
        if not self.clock_domains:
            raise SemanticError(f"{kind} '{name}' requires a clock and reset")
        domains_by_clock = {
            domain.clock: domain for domain in self.clock_domains
        }
        if requested is not None:
            if requested not in domains_by_clock:
                raise SemanticError(
                    f"{kind} '{name}' references unknown clock domain "
                    f"'{requested}'",
                    code="ZL-DOMAIN-UNKNOWN",
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
        if len(set(module.clocks)) != len(module.clocks):
            raise SemanticError("duplicate clock declaration")
        resets = module.reset_domains or tuple(
            (name, None) for name in module.resets
        )
        if len({name for name, _ in resets}) != len(resets):
            raise SemanticError("duplicate reset declaration")
        if bool(module.clocks) != bool(resets):
            raise SemanticError("clock and reset must be declared together")
        clocks = module.clock_physical or tuple(
            ast.ClockPhysicalDecl(name) for name in module.clocks
        )
        reset_specs = module.reset_physical or tuple(
            ast.ResetPhysicalDecl(name, domain) for name, domain in resets
        )
        if tuple(item.name for item in clocks) != module.clocks:
            raise SemanticError(
                "physical clock declarations must match clock declarations"
            )
        if tuple(item.name for item in reset_specs) != module.resets:
            raise SemanticError(
                "physical reset declarations must match reset declarations"
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
                    "a single-clock module requires exactly one reset"
                )
            default_clock = module.clocks[0]
            default_reset, reset_domain = resets[0]
            if reset_domain is not None and reset_domain != default_clock:
                raise SemanticError(
                    f"reset '{default_reset}' references unknown clock domain "
                    f"'{reset_domain}'"
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
                        f"reset '{reset_name}' requires an explicit clock domain"
                    )
                if reset_domain not in module.clocks:
                    raise SemanticError(
                        f"reset '{reset_name}' references unknown clock domain "
                        f"'{reset_domain}'"
                    )
                if reset_domain in by_clock:
                    raise SemanticError(
                        f"clock domain '{reset_domain}' has more than one reset"
                    )
                by_clock[reset_domain] = reset_name
            missing = set(module.clocks) - by_clock.keys()
            if missing:
                raise SemanticError(
                    f"clock domain '{sorted(missing)[0]}' has no reset"
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
            raise SemanticError("clock and reset must have different names")
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


class HierarchyDependencyValidator:
    """Reject current-cycle cycles through typed child outputs."""

    def validate(
        self,
        child_irs: dict[str, ir_module.Module],
        bindings: tuple[ir_module.InstancePortBinding, ...],
        locals_: tuple[ir_module.LocalValue, ...],
        memories: tuple[ir_storage.Memory, ...] = (),
        fifos: tuple[ir_storage.Fifo, ...] = (),
    ) -> None:
        """Reject current-cycle cycles across scalar child instance boundaries.

        An InstanceOutputRef is a read-only value, but a combinational child's
        output can still depend on one of its input bindings.  Model exactly that
        typed dependency relation.  Registers, latency-one storage and explicit
        delay/pipeline nodes terminate the current-cycle walk.  A latency-zero
        memory instead exposes the exact controls that affect its current read,
        so asynchronous-read hierarchy cannot hide a combinational loop.
        """

        local_values = {item.name: item.expression for item in locals_}
        parent_memories = {item.name: item for item in memories}
        parent_fifos = {item.name: item for item in fifos}

        def fifo_observation_controls(
            reference: ir_expr.FifoRef,
            resources: dict[str, ir_storage.Fifo],
        ) -> tuple[ir_expr.Expression, ...]:
            """Return only current-cycle controls observed by a FIFO signal.

            A scheduled FIFO publishes state-derived observations.  A legacy
            globally controlled FIFO additionally exposes accepted-pop lookahead
            through ``ready`` and request diagnostics through overflow/underflow.
            Keep the state observations as cycle cuts while following exactly the
            legacy control expressions used by the typed simulator and backends.
            """

            fifo = resources.get(reference.fifo)
            if fifo is None or fifo.scheduled:
                return ()
            controls: tuple[ir_expr.Expression | None, ...]
            if reference.signal is ir_storage.FifoSignal.READY:
                controls = (fifo.pop,)
            elif reference.signal is ir_storage.FifoSignal.OVERFLOW:
                controls = (fifo.push, fifo.pop)
            elif reference.signal is ir_storage.FifoSignal.UNDERFLOW:
                controls = (fifo.pop,)
            else:
                controls = ()
            return tuple(item for item in controls if item is not None)

        def references(
            expression: ir_expr.Expression,
            *,
            local_expressions: dict[str, ir_expr.Expression],
            memory_resources: dict[str, ir_storage.Memory],
            fifo_resources: dict[str, ir_storage.Fifo],
            known_inputs: set[str] | None = None,
            instance_inputs: Callable[
                [ir_expr.InstanceOutputRef], Iterable[ir_expr.Expression]
            ] | None = None,
            active_locals: frozenset[str] = frozenset(),
        ) -> tuple[set[str], set[tuple[str, str]]]:
            input_names: set[str] = set()
            instance_outputs: set[tuple[str, str]] = set()

            def visit(
                value: object,
                active: frozenset[str],
                active_memories: frozenset[str] = frozenset(),
                active_fifos: frozenset[str] = frozenset(),
            ) -> None:
                if isinstance(value, ir_expr.InputRef):
                    replacement = local_expressions.get(value.name)
                    if replacement is not None:
                        if value.name in active:
                            raise SemanticError(
                                f"cyclic immutable local '{value.name}' in child "
                                "dependency analysis"
                            )
                        visit(
                            replacement,
                            active | {value.name},
                            active_memories,
                            active_fifos,
                        )
                    elif known_inputs is None or value.name in known_inputs:
                        input_names.add(value.name)
                    return
                if isinstance(value, ir_expr.InstanceOutputRef):
                    if instance_inputs is None:
                        instance_outputs.add((value.instance, value.port))
                    else:
                        for binding in instance_inputs(value):
                            visit(
                                binding,
                                active,
                                active_memories,
                                active_fifos,
                            )
                    return
                if isinstance(value, ir_expr.MemoryRef):
                    memory = memory_resources.get(value.memory)
                    if (
                        value.signal is ir_storage.MemorySignal.READ_DATA
                        and memory is not None
                        and memory.read_latency == 0
                        and memory.name not in active_memories
                    ):
                        controls = [memory.read_address]
                        if memory.collision is ir_storage.MemoryCollision.WRITE_FIRST:
                            controls.extend((
                                memory.write_enable,
                                memory.write_address,
                                memory.write_data,
                                memory.write_mask,
                            ))
                        nested_active = active_memories | {memory.name}
                        for control in controls:
                            if control is not None:
                                visit(control, active, nested_active, active_fifos)
                    return
                if isinstance(value, ir_expr.FifoRef):
                    if value.fifo not in active_fifos:
                        nested_active = active_fifos | {value.fifo}
                        for control in fifo_observation_controls(
                            value, fifo_resources
                        ):
                            visit(
                                control,
                                active,
                                active_memories,
                                nested_active,
                            )
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
                            active_memories,
                            active_fifos,
                        )

            visit(expression, active_locals)
            return input_names, instance_outputs

        output_summary_cache: dict[int, dict[str, frozenset[str]]] = {}
        active_summary_modules: set[int] = set()

        def output_input_dependencies(
            current: ir_module.Module,
        ) -> dict[str, frozenset[str]]:
            """Summarize each wire output in terms of this module's inputs.

            A direct child output is not itself an input dependency.  Resolve it
            through that child's already typed output summary and exact scalar
            bindings.  This makes the summary transitive across an arbitrary
            bounded hierarchy while registers and latency-one storage remain
            explicit current-cycle cuts.
            """

            key = id(current)
            cached = output_summary_cache.get(key)
            if cached is not None:
                return cached
            if key in active_summary_modules:
                raise SemanticError(
                    f"cyclic typed child hierarchy while summarizing '{current.name}'"
                )
            active_summary_modules.add(key)
            try:
                current_inputs = {item.name for item in current.inputs}
                current_locals = {
                    item.name: item.expression for item in current.locals
                }
                current_memories = {
                    item.name: item for item in current.memories
                }
                current_fifos = {
                    item.name: item for item in current.fifos
                }
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
                    for item in current.instance_bindings
                }

                def nested_instance_inputs(
                    reference: ir_expr.InstanceOutputRef,
                ) -> Iterable[ir_expr.Expression]:
                    nested = current_children.get(reference.instance)
                    if nested is None:
                        return ()
                    nested_summary = output_input_dependencies(nested)
                    return tuple(
                        binding
                        for nested_input in nested_summary.get(reference.port, ())
                        if (
                            binding := current_bindings.get(
                                (reference.instance, nested_input)
                            )
                        ) is not None
                    )

                def summarize_expression(
                    expression: ir_expr.Expression,
                ) -> set[str]:
                    dependencies, _ = references(
                        expression,
                        local_expressions=current_locals,
                        memory_resources=current_memories,
                        fifo_resources=current_fifos,
                        known_inputs=current_inputs,
                        instance_inputs=nested_instance_inputs,
                    )
                    return dependencies

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

                summary: dict[str, frozenset[str]] = {}
                for output in current.outputs:
                    if output.protocol is not InterfaceProtocol.WIRE:
                        continue
                    dependencies: set[str] = set()
                    dependencies.update(*(
                        summarize_expression(assignment.expression)
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
                active_summary_modules.remove(key)

        binding_by_input = {
            (item.instance, item.port): item.expression for item in bindings
        }
        graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for instance, child in child_irs.items():
            # The declaration-name alias and each physical name point at the same
            # child IR.  Only physical instances that own bindings participate.
            if not any(item.instance == instance for item in bindings):
                continue
            child_output_dependencies = output_input_dependencies(child)
            for output in child.outputs:
                if output.protocol is not InterfaceProtocol.WIRE:
                    continue
                dependencies: set[tuple[str, str]] = set()
                for input_name in child_output_dependencies.get(output.name, ()):
                    bound = binding_by_input.get((instance, input_name))
                    if bound is None:
                        continue
                    _, referenced_outputs = references(
                        bound,
                        local_expressions=local_values,
                        memory_resources=parent_memories,
                        fifo_resources=parent_fifos,
                    )
                    dependencies.update(referenced_outputs)
                graph[(instance, output.name)] = dependencies

        reject_dependency_cycles(
            graph,
            render_node=lambda node: f"{node[0]}.{node[1]}",
            description="child",
        )
