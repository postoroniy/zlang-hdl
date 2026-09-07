"""Source-defined target/resource loading and bounded manual architecture mapping."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from enum import Enum
from functools import lru_cache
from hashlib import sha256
from typing import Iterable

from zlang.ast import nodes as ast
from zlang.ir import expressions as expr
from zlang.ir.module import Assignment, Module, NextAssignment, PortDirection, Register
from zlang.ir.target import (
    ArchitectureTemplate,
    DedicatedPhysicalEdge,
    ImplementationGraph,
    PhysicalBinding,
    PipelineConfiguration,
    PipelineSite,
    ResourceDedicatedLink,
    ResourceDefinition,
    ResourceInstance,
    ResourcePort,
    ResourceRegisterSite,
    SemanticPortMapping,
    TargetFamilyDefinition,
    TargetInstance,
)
from zlang.ir.types import FixedType, VecType
from zlang.ir.timing import TimingKnowledge
from zlang.ir.signed_reductions import (
    ProductTermSign,
    SignedProductReduction,
    recognize_signed_product_reduction,
)
from zlang.stdlib import available_stdlib_modules, resolve_stdlib


class ArchitectureSelectionMode(str, Enum):
    GENERIC = "generic"
    PREFERRED = "preferred"
    REQUIRED = "required"


class TargetArchitectureError(ValueError):
    """A source-defined target or manually requested architecture is illegal."""


def validate_pipeline_configuration(
    resource: ResourceDefinition, name: str,
) -> PipelineConfiguration:
    try:
        configuration = resource.pipeline_configuration(name)
    except ValueError as error:
        raise TargetArchitectureError(str(error)) from error
    sites = {item.name: item for item in resource.pipeline_sites}
    if any(site not in sites for site in configuration.sites):
        raise TargetArchitectureError(
            f"resource '{resource.identity}' configuration '{name}' uses an unknown pipeline site"
        )
    if sum(sites[site].latency_delta for site in configuration.sites) != configuration.latency:
        raise TargetArchitectureError(
            f"resource '{resource.identity}' configuration '{name}' has inconsistent latency"
        )
    return configuration


def validate_memory_configuration(
    resource: ResourceDefinition, *, width: int, depth: int, port_mode: str,
) -> None:
    if resource.resource_class not in {"block_memory", "distributed_memory"}:
        raise TargetArchitectureError(f"resource '{resource.identity}' is not memory")
    capabilities = dict(resource.capabilities)
    modes = capabilities.get("port_modes", "").split(".")
    widths = {int(value) for value in capabilities.get("widths", "").split(".") if value}
    if port_mode not in modes:
        raise TargetArchitectureError(
            f"memory resource '{resource.identity}' does not support port mode '{port_mode}'"
        )
    if widths and width not in widths:
        raise TargetArchitectureError(
            f"memory resource '{resource.identity}' does not support width {width}"
        )
    capacity = int(capabilities.get("capacity_bits", "0"))
    if capacity and width * depth > capacity:
        raise TargetArchitectureError(
            f"memory resource '{resource.identity}' capacity exceeded: {width * depth}>{capacity} bits"
        )


def validate_clock_requirement(
    resource: ResourceDefinition, *, input_mhz: int, output_mhz: int,
    outputs: int = 1,
) -> None:
    if resource.resource_class != "clock_generator":
        raise TargetArchitectureError(f"resource '{resource.identity}' is not a clock generator")
    values = dict(resource.capabilities)
    for value, low_key, high_key, label in (
        (input_mhz, "input_frequency_min_mhz", "input_frequency_max_mhz", "input"),
        (output_mhz, "output_frequency_min_mhz", "output_frequency_max_mhz", "output"),
    ):
        if low_key in values and value < int(values[low_key]) or high_key in values and value > int(values[high_key]):
            raise TargetArchitectureError(
                f"clock resource '{resource.identity}' rejects {label} frequency {value} MHz"
            )
    if outputs > int(values.get("outputs", "1")):
        raise TargetArchitectureError(
            f"clock resource '{resource.identity}' provides too few outputs"
        )


def validate_inventory(
    target: TargetInstance, requirements: tuple[tuple[str, int], ...],
) -> None:
    available = dict(target.inventory)
    for identity, count in requirements:
        if available.get(identity, 0) < count:
            raise TargetArchitectureError(
                f"target '{target.identity}' resource inventory exceeded: requires {count} "
                f"'{identity}', provides {available.get(identity, 0)}"
            )


def _definition_identity(path: str, name: str) -> str:
    return f"{path}.{name}"


def _dependency_hashes(items: Iterable[object]) -> tuple[tuple[str, str], ...]:
    return tuple((item.path, item.digest) for item in items)


def _resource_from_decl(item, declaration: ast.ResourceDefinitionDecl, dependencies) -> ResourceDefinition:
    sites = tuple(PipelineSite(
        value.name, value.semantic_location, value.latency_delta,
        value.initiation_interval, value.resource_local, value.estimated_delay_ps,
    ) for value in declaration.pipeline_sites)
    configurations = tuple(PipelineConfiguration(
        value.name, value.sites, value.latency, value.initiation_interval,
        value.physical_settings,
    ) for value in declaration.pipeline_configurations)
    site_names = {value.name for value in sites}
    if len(site_names) != len(sites):
        raise TargetArchitectureError(f"resource '{declaration.name}' has duplicate pipeline sites")
    for configuration in configurations:
        unknown = tuple(name for name in configuration.sites if name not in site_names)
        if unknown:
            raise TargetArchitectureError(
                f"resource '{declaration.name}' pipeline configuration '{configuration.name}' "
                f"uses unknown site '{unknown[0]}'"
            )
        expected_latency = sum(value.latency_delta for value in sites if value.name in configuration.sites)
        if configuration.latency != expected_latency:
            raise TargetArchitectureError(
                f"resource '{declaration.name}' pipeline configuration '{configuration.name}' "
                f"latency {configuration.latency} does not match active-site latency {expected_latency}"
            )
    bindings = tuple(PhysicalBinding(
        backend, emitter, declaration.physical_primitive,
        declaration.physical_site_bindings, declaration.physical_edge_bindings,
    ) for backend, emitter in declaration.bindings)
    return ResourceDefinition(
        identity=_definition_identity(item.path, declaration.name), name=declaration.name,
        source_path=item.path, source_hash=item.digest,
        dependency_hashes=_dependency_hashes(dependencies),
        ports=tuple(ResourcePort(p.name, p.direction, p.signedness, p.width) for p in declaration.ports),
        operation=declaration.operation, limits=declaration.limits,
        register_sites=tuple(ResourceRegisterSite(r.name, r.minimum, r.maximum) for r in declaration.register_sites),
        dedicated_links=tuple(ResourceDedicatedLink(
            link.name, link.source_port, link.destination_port,
            link.width, link.relation, 0, link.fabric_fallback,
        ) for link in declaration.dedicated_links),
        backend_bindings=declaration.bindings,
        resource_class=declaration.resource_class,
        capabilities=declaration.capabilities,
        pipeline_sites=sites, pipeline_configurations=configurations,
        physical_bindings=bindings, source_origin=declaration.origin,
    )


def _catalog(prefixes: tuple[str, ...]):
    paths = tuple(path for path in available_stdlib_modules() if path.startswith(prefixes))
    sources = resolve_stdlib(paths)
    by_path = {item.path: item for item in sources}

    def dependencies_for(item):
        ordered = []
        seen = set()

        def visit(path):
            if path in seen:
                return
            seen.add(path)
            dependency = by_path.get(path)
            if dependency is None:
                return
            for child in dependency.dependencies:
                visit(child)
            ordered.append(dependency)

        for dependency in item.dependencies:
            visit(dependency)
        return tuple(ordered)

    resources: dict[str, ResourceDefinition] = {}
    resource_names: dict[str, list[str]] = {}
    for item in sources:
        for declaration in item.ast.resource_definitions:
            value = _resource_from_decl(item, declaration, dependencies_for(item))
            if value.identity in resources:
                raise TargetArchitectureError(f"duplicate resource '{value.identity}'")
            resources[value.identity] = value
            resource_names.setdefault(value.name, []).append(value.identity)

    def resource_identity(name: str) -> str:
        matches = resource_names.get(name, [])
        if len(matches) != 1:
            description = "unknown" if not matches else "ambiguous"
            raise TargetArchitectureError(f"{description} resource '{name}'")
        return matches[0]

    families: dict[str, TargetFamilyDefinition] = {}
    family_names: dict[str, list[str]] = {}
    for item in sources:
        for declaration in item.ast.target_families:
            identity = _definition_identity(item.path, declaration.name)
            value = TargetFamilyDefinition(
                identity, declaration.name, item.path, item.digest,
                _dependency_hashes(dependencies_for(item)),
                tuple(resource_identity(name) for name in declaration.resources),
                declaration.origin,
            )
            families[identity] = value
            family_names.setdefault(value.name, []).append(identity)

    def family_identity(name: str) -> str:
        matches = family_names.get(name, [])
        if len(matches) != 1:
            description = "unknown" if not matches else "ambiguous"
            raise TargetArchitectureError(f"{description} target family '{name}'")
        return matches[0]

    targets: dict[str, TargetInstance] = {}
    for item in sources:
        for declaration in item.ast.target_instances:
            family = families[family_identity(declaration.family)]
            family_resources = {resources[identity].name: identity for identity in family.resource_identities}
            try:
                inventory = tuple((family_resources[name], count) for name, count in declaration.inventory)
                capacities = tuple(
                    (family_resources[name], link, count)
                    for name, link, count in declaration.dedicated_capacities
                )
            except KeyError as error:
                raise TargetArchitectureError(
                    f"target '{declaration.name}' references unavailable resource '{error.args[0]}'"
                ) from error
            identity = _definition_identity(item.path, declaration.name)
            targets[identity] = TargetInstance(
                identity, declaration.name, declaration.part, family.identity,
                item.path, item.digest, _dependency_hashes(dependencies_for(item)), inventory,
                capacities, declaration.origin,
            )

    architectures: dict[str, ArchitectureTemplate] = {}
    for item in sources:
        for declaration in item.ast.architecture_templates:
            identity = _definition_identity(item.path, declaration.name)
            architectures[identity] = ArchitectureTemplate(
                identity, declaration.name, item.path, item.digest,
                _dependency_hashes(dependencies_for(item)), declaration.operation,
                declaration.resource, declaration.resource_count,
                declaration.latency, declaration.initiation_interval,
                declaration.register_configuration, declaration.pipeline_configuration,
                declaration.dedicated_link,
                declaration.origin,
            )
    return resources, families, targets, architectures


def load_target(selection: str) -> tuple[TargetInstance, TargetFamilyDefinition, tuple[ResourceDefinition, ...]]:
    resources, families, targets, _ = _catalog(("std.target.",))
    matches = tuple(value for value in targets.values() if selection in {
        value.identity, value.name, value.part,
        value.name.replace("_", "-"),
    })
    if len(matches) != 1:
        raise TargetArchitectureError(f"unknown target '{selection}'")
    target = matches[0]
    family = families[target.family_identity]
    return target, family, tuple(resources[identity] for identity in family.resource_identities)


def load_architecture(selection: str) -> ArchitectureTemplate:
    _, _, _, architectures = _catalog(("std.target.", "std.arch."))
    matches = tuple(value for value in architectures.values() if selection in {
        value.identity, value.name,
    })
    if len(matches) != 1:
        raise TargetArchitectureError(f"unknown architecture '{selection}'")
    return matches[0]


def load_architecture_templates(*, operation: str | None = None) -> tuple[ArchitectureTemplate, ...]:
    """Return source-defined templates in stable identity order."""
    _, _, _, architectures = _catalog(("std.arch.",))
    values = tuple(
        item for item in architectures.values()
        if operation is None or item.operation == operation
    )
    return tuple(sorted(values, key=lambda item: item.identity))


def generic_implementation_graph(module: Module, target: TargetInstance | None = None) -> ImplementationGraph:
    semantic = sha256(_semantic_payload(module).encode()).hexdigest()
    latency_knowledge, latency = _module_implementation_latency(module)
    return ImplementationGraph(
        semantic_region_identity=semantic,
        architecture_template_identity="std.arch.generic",
        target_identity=target.identity if target else None,
        target_hash=target.source_hash if target else None,
        resource_definition_hashes=(), resources=(), dedicated_edges=(),
        latency=latency, initiation_interval=1,
        realization_backend="backend_independent",
        latency_knowledge=latency_knowledge,
        legality_evidence=("technology-independent generic implementation",),
        target_dependency_hashes=target.dependency_hashes if target else (),
        selection_policy=ArchitectureSelectionMode.GENERIC.value,
        target_part=target.part if target else None,
    )


def _module_implementation_latency(module: Module) -> tuple[str, int]:
    """Translate semantic output timing into the integer graph compatibility ABI.

    ``ImplementationGraph.latency`` predates public module timing and is an
    integer consumed by M28/M31 cost code.  Preserve that field while carrying
    the knowledge class separately, so an unknown stateful output is never
    described as a proven zero-cycle result.
    """
    contract = getattr(module, "timing_contract", None)
    if contract is not None:
        return TimingKnowledge.KNOWN.value, contract.latency

    output_timings = tuple(getattr(module, "output_timings", ()))
    if output_timings:
        unknown = tuple(
            item for item in output_timings
            if item.timing.knowledge is TimingKnowledge.UNKNOWN
        )
        if unknown:
            return TimingKnowledge.UNKNOWN.value, 0
        known = {
            item.timing.latency
            for item in output_timings
            if item.timing.knowledge is TimingKnowledge.KNOWN
        }
        if len(known) > 1:
            raise TargetArchitectureError(
                "module outputs have inconsistent derived implementation latency"
            )
        if known:
            return TimingKnowledge.KNOWN.value, next(iter(known))
        return TimingKnowledge.TIMELESS.value, 0

    if (
        module.has_protocol_interfaces
        or module.hierarchical_connections
        or module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
        or module.request_responses
        or module.csr_blocks
    ):
        # Legacy/uncontracted stateful modules may predate derived output
        # records.  Preserve their uncertainty instead of reviving the old
        # misleading "latency zero" claim.
        return TimingKnowledge.UNKNOWN.value, 0

    # Compatibility for old hand-built typed modules without timing records.
    # The shared traversal is recursive, so explicit delays/pipelines nested
    # below conversions and arithmetic no longer collapse to zero.
    from zlang.timing import timing_info

    assignment_latencies = tuple(
        timing_info(item.expression, module=module).latency
        for item in module.assignments
    )
    return (
        TimingKnowledge.KNOWN.value,
        max(assignment_latencies, default=0),
    )


def select_implementation_graph(
    module: Module,
    *,
    target: str | None = None,
    architecture: str | None = None,
    mode: ArchitectureSelectionMode | str = ArchitectureSelectionMode.GENERIC,
) -> ImplementationGraph:
    policy = ArchitectureSelectionMode(mode)
    selected_target = None
    family = None
    resources: tuple[ResourceDefinition, ...] = ()
    if target is not None:
        selected_target, family, resources = load_target(target)
    generic = generic_implementation_graph(module, selected_target)
    if policy is ArchitectureSelectionMode.GENERIC:
        return generic
    if architecture is None:
        if policy is ArchitectureSelectionMode.REQUIRED:
            raise TargetArchitectureError("required architecture is unavailable: no architecture was selected")
        return replace(
            generic, selection_policy=policy.value,
            legality_evidence=(
                *generic.legality_evidence,
                "no manual architecture was selected",
            ),
        ) if policy is ArchitectureSelectionMode.PREFERRED else generic
    if selected_target is None:
        error = TargetArchitectureError(
            f"architecture '{architecture}' requires an explicit target"
        )
        if policy is ArchitectureSelectionMode.PREFERRED:
            return replace(
                generic, selection_policy=policy.value,
                legality_evidence=(*generic.legality_evidence, f"preferred architecture rejected: {error}"),
            )
        raise error
    try:
        template = load_architecture(architecture)
        return _map_manual(module, selected_target, family, resources, template, policy)
    except TargetArchitectureError as error:
        if policy is ArchitectureSelectionMode.PREFERRED:
            return replace(
                generic, selection_policy=policy.value,
                legality_evidence=(*generic.legality_evidence, f"preferred architecture rejected: {error}"),
            )
        raise


def _map_manual(module, target, family, resources, template,
                policy=ArchitectureSelectionMode.REQUIRED) -> ImplementationGraph:
    if template.operation == "synchronous_memory":
        return _map_synchronous_memory(module, target, family, resources, template, policy)
    if template.operation != "symmetric_fir_cascade":
        raise TargetArchitectureError(
            f"architecture '{template.identity}' has unsupported manual operation '{template.operation}'"
        )
    matches = tuple(item for item in resources if item.name == template.resource_name)
    if len(matches) != 1:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires unavailable resource '{template.resource_name}'"
        )
    resource = matches[0]
    if resource.identity not in family.resource_identities:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' resource '{resource.identity}' is unavailable on target '{target.identity}'"
        )
    inventory = dict(target.inventory).get(resource.identity, 0)
    if inventory < template.resource_count:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' resource inventory exceeded: requires "
            f"{template.resource_count} {resource.name}, target provides {inventory}"
        )
    if template.resource_count != 4:
        raise TargetArchitectureError("the bounded symmetric FIR template requires exactly four resources")
    link = next((item for item in resource.dedicated_links
                 if item.name == template.dedicated_link), None)
    if link is None:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires unavailable dedicated connection "
            f"'{template.dedicated_link}'"
        )
    capacity = dict(((rid, kind), count) for rid, kind, count in target.dedicated_capacities).get(
        (resource.identity, link.name), 0
    )


    required_edges = template.resource_count - 1
    if capacity < required_edges:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' dedicated connection '{link.name}' "
            f"requires capacity {required_edges}, target provides {capacity}"
        )
    for name, value in template.register_configuration:
        site = next((item for item in resource.register_sites if item.name == name), None)
        if site is None or not site.minimum <= value <= site.maximum:
            supported = "unavailable" if site is None else f"{site.minimum}..{site.maximum}"
            raise TargetArchitectureError(
                f"architecture '{template.identity}' register site '{name}' value {value} is illegal; supported {supported}"
            )
    selected_pipeline = None
    if template.pipeline_configuration is not None:
        try:
            selected_pipeline = resource.pipeline_configuration(template.pipeline_configuration)
        except ValueError as error:
            raise TargetArchitectureError(str(error)) from error
        if selected_pipeline.initiation_interval != template.initiation_interval:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' II {template.initiation_interval} does not match "
                f"pipeline configuration II {selected_pipeline.initiation_interval}"
            )
    pairs, quantization, accumulator, output_register, semantic_latency = _recognize_symmetric_fir(module, template)
    evidence = _validate_widths(template, resource, pairs, accumulator)
    if template.latency != semantic_latency:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' latency {template.latency} does not match "
            f"semantic fixed latency {semantic_latency}"
        )
    configuration_latency = selected_pipeline.latency if selected_pipeline is not None else 0
    if semantic_latency != configuration_latency + (1 if output_register is not None else 0):
        raise TargetArchitectureError(
            f"architecture '{template.identity}' pipeline configuration contributes "
            f"{configuration_latency} cycles but semantic output boundary requires {semantic_latency}"
        )
    nodes: list[ResourceInstance] = []
    previous = "constant:zero"
    for index, (left, right, coefficient) in enumerate(pairs):
        identity = f"dsp{index}"
        stage_result = "value:" + sha256(
            f"{previous}|{_expression_identity(coefficient)}|{_expression_identity(left)}|{_expression_identity(right)}".encode()
        ).hexdigest()
        mappings = (
            SemanticPortMapping("a", _semantic_mapping_identity(left), left),
            SemanticPortMapping("d", _semantic_mapping_identity(right), right),
            SemanticPortMapping("b", _semantic_mapping_identity(coefficient), coefficient),
            SemanticPortMapping("pcin", previous),
            SemanticPortMapping("p", stage_result),
            SemanticPortMapping("pcout", stage_result),
        )
        physical_configuration = (
            selected_pipeline.physical_settings if selected_pipeline is not None
            else template.register_configuration
        )
        configuration = tuple((*physical_configuration,
                               ("pipeline_configuration", selected_pipeline.name if selected_pipeline else "legacy"),
                               ("sample_left_index", left.index),
                               ("sample_right_index", right.index),
                               ("coefficient_index", coefficient.index)))
        nodes.append(ResourceInstance(
            identity, resource.identity, resource.operation, configuration, mappings,
        ))
        previous = stage_result
    edges = tuple(DedicatedPhysicalEdge(
        f"pcascade:{index}:{index + 1}", link.name,
        f"dsp{index}", link.source_port, f"dsp{index + 1}",
        link.destination_port, link.width, link.placement_relation, 0,
        link.fabric_fallback,
    ) for index in range(3))
    semantic_identity = _expression_identity(accumulator)
    return ImplementationGraph(
        semantic_region_identity=semantic_identity,
        architecture_template_identity=template.identity,
        target_identity=target.identity, target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=tuple(nodes), dedicated_edges=edges,
        latency=template.latency,
        initiation_interval=template.initiation_interval,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        quantization=quantization, source_origin=quantization.origin,
        legality_evidence=tuple(evidence),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy=ArchitectureSelectionMode(policy).value,
        target_part=target.part,
        pipeline_configuration_identity=(
            f"{resource.identity}.{selected_pipeline.name}" if selected_pipeline else None
        ),
        active_pipeline_sites=selected_pipeline.sites if selected_pipeline else (),
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )


def _map_synchronous_memory(module, target, family, resources, template, policy):
    if len(module.memories) != 1 or template.resource_count != 1:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires exactly one semantic memory and one resource"
        )
    matches = tuple(item for item in resources if item.name == template.resource_name)
    if len(matches) != 1:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires unavailable resource '{template.resource_name}'"
        )
    resource = matches[0]
    validate_inventory(target, ((resource.identity, 1),))
    memory = module.memories[0]
    if memory.scheduled:
        raise TargetArchitectureError(
            "target memory mapping does not support rule-owned scheduled memory"
        )
    if memory.read_latency != 1:
        raise TargetArchitectureError(
            "target memory mapping supports only one-cycle synchronous reads"
        )
    if (
        memory.contents_reset.value != "clear"
        or memory.read_data_reset.value != "clear"
    ):
        raise TargetArchitectureError(
            "target memory mapping does not advertise reset-preserved contents "
            "or read data"
        )
    if memory.write_mask_width is not None:
        raise TargetArchitectureError(
            "target memory mapping does not support byte write masks"
        )
    validate_memory_configuration(
        resource, width=memory.element_type.width, depth=memory.depth, port_mode="simple_dual",
    )
    configuration = validate_pipeline_configuration(
        resource, template.pipeline_configuration or "core_registered",
    )
    total_latency = memory.read_latency + configuration.latency
    if template.latency != total_latency:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' latency {template.latency} does not match "
            f"memory/configuration latency {total_latency}"
        )
    node = ResourceInstance(
        "memory0", resource.identity, resource.operation,
        tuple((*configuration.physical_settings,
               ("pipeline_configuration", configuration.name),
               ("depth", memory.depth), ("width", memory.element_type.width))),
        (
            SemanticPortMapping("read_address", f"memory:{memory.name}:read_address", memory.read_address),
            SemanticPortMapping("write_enable", f"memory:{memory.name}:write_enable", memory.write_enable),
            SemanticPortMapping("write_address", f"memory:{memory.name}:write_address", memory.write_address),
            SemanticPortMapping("write_data", f"memory:{memory.name}:write_data", memory.write_data),
            SemanticPortMapping("read_data", f"memory:{memory.name}:read_data"),
        ),
    )
    return ImplementationGraph(
        semantic_region_identity=sha256(_semantic_payload(memory).encode()).hexdigest(),
        architecture_template_identity=template.identity,
        target_identity=target.identity, target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=(node,), dedicated_edges=(), latency=total_latency,
        initiation_interval=template.initiation_interval,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        legality_evidence=(
            f"single synchronous memory {memory.depth}x{memory.element_type.width}",
            f"pipeline configuration {configuration.name}",
        ),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy=ArchitectureSelectionMode(policy).value,
        target_part=target.part,
        pipeline_configuration_identity=f"{resource.identity}.{configuration.name}",
        active_pipeline_sites=configuration.sites,
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )


def map_manual_architecture(
    module: Module,
    target: TargetInstance,
    family: TargetFamilyDefinition,
    resources: tuple[ResourceDefinition, ...],
    template: ArchitectureTemplate,
) -> ImplementationGraph:
    """Map one explicitly requested template; public for tooling and diagnostics."""
    return _map_manual(module, target, family, resources, template)


def map_auto_symmetric_configuration(
    module: Module,
    target: TargetInstance,
    family: TargetFamilyDefinition,
    resources: tuple[ResourceDefinition, ...],
    template: ArchitectureTemplate,
    configuration: PipelineConfiguration,
    *,
    exact_latency: int | None = None,
) -> ImplementationGraph:
    """Map one resource-published configuration for the bounded auto FIR slice.

    The temporary register chain is an adapter into the already-validated typed
    manual mapper.  It is not backend RTL and is never emitted.  The returned
    graph receives its real resource-local/compensation timing DAG below.
    """
    explorations = tuple(
        item for item in module.pipeline_explorations
        if isinstance(item.source_expression, expr.FixedConvert)
    )
    if len(explorations) != 1:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires one typed fixed pipeline(auto) region"
        )
    exploration = explorations[0]
    conversion = exploration.source_expression
    output = next((item for item in module.outputs if item.name == exploration.output), None)
    if output is None:
        raise TargetArchitectureError(
            f"pipeline(auto) output '{exploration.output}' is unavailable"
        )
    useful_latency = 1 + configuration.latency
    registers = tuple(Register(
        f"__target_auto_q{index}", conversion.type,
        expr.Constant(0, conversion.type),
    ) for index in range(useful_latency))
    next_assignments = [NextAssignment(registers[0], conversion)]
    next_assignments.extend(
        NextAssignment(registers[index], expr.RegisterRef(
            registers[index - 1].name, conversion.type,
        ))
        for index in range(1, useful_latency)
    )
    assignments = tuple(
        item for item in module.assignments
        if not (hasattr(item.target, "name") and item.target.name == output.name)
    ) + (Assignment(output, expr.RegisterRef(registers[-1].name, conversion.type)),)
    adapter = replace(
        module,
        assignments=assignments,
        registers=(*module.registers, *registers),
        next_assignments=(*module.next_assignments, *next_assignments),
    )
    configured_template = replace(
        template,
        latency=useful_latency,
        initiation_interval=configuration.initiation_interval,
        pipeline_configuration=configuration.name,
    )
    graph = _map_manual(
        adapter, target, family, resources, configured_template,
        ArchitectureSelectionMode.PREFERRED,
    )
    resource = next(
        item for item in resources
        if item.identity == graph.resources[0].resource_definition_identity
    )
    from zlang.target_timing import build_dsp_cascade_timing_dag
    timing_dag = build_dsp_cascade_timing_dag(
        graph, resource, configuration, structural_latency=1,
        output_width=conversion.type.width, exact_latency=exact_latency,
    )
    return replace(
        graph,
        latency=timing_dag.output_latency,
        timing_dag=timing_dag,
        selection_policy="auto",
    )


def map_auto_signed_product_configuration(
    module: Module,
    target: TargetInstance,
    family: TargetFamilyDefinition,
    resources: tuple[ResourceDefinition, ...],
    template: ArchitectureTemplate,
    configuration: PipelineConfiguration,
    *,
    exact_latency: int | None = None,
) -> ImplementationGraph:
    """Map an ordered exact signed-product reduction without changing its tree."""

    if template.operation != "signed_product_reduction":
        raise TargetArchitectureError(
            f"architecture '{template.identity}' is not a signed-product reduction"
        )
    explorations = tuple(
        item for item in module.pipeline_explorations
        if isinstance(item.source_expression, expr.FixedConvert)
    )
    if len(explorations) != 1:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires one typed fixed pipeline(auto) region"
        )
    exploration = explorations[0]
    conversion = exploration.source_expression
    reduction = recognize_signed_product_reduction(conversion.expression)
    if reduction is None:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires one exact signed-product reduction"
        )
    if not isinstance(reduction.result_type, FixedType) or any(
        not isinstance(term.product_type, FixedType) for term in reduction.terms
    ):
        raise TargetArchitectureError(
            f"architecture '{template.identity}' physical mapping supports signed FixedType only"
        )
    if template.resource_count != len(reduction.terms):
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires {template.resource_count} products, "
            f"typed reduction has {len(reduction.terms)}"
        )

    matches = tuple(item for item in resources if item.name == template.resource_name)
    if len(matches) != 1:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires unavailable resource '{template.resource_name}'"
        )
    resource = matches[0]
    if resource.identity not in family.resource_identities:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' resource '{resource.identity}' is unavailable on target '{target.identity}'"
        )
    validate_inventory(target, ((resource.identity, len(reduction.terms)),))
    capabilities = dict(resource.capabilities)
    modes = set(capabilities.get("accumulator_modes", "").split("."))
    required_modes = {"accumulator_plus_product"}
    if reduction.has_subtraction:
        required_modes.add("accumulator_minus_product")
    missing_modes = sorted(required_modes - modes)
    if missing_modes:
        raise TargetArchitectureError(
            f"resource '{resource.identity}' lacks signed reduction capability '{missing_modes[0]}'"
        )
    if capabilities.get("signed") != "true":
        raise TargetArchitectureError(
            f"resource '{resource.identity}' does not advertise signed arithmetic"
        )
    configuration = validate_pipeline_configuration(resource, configuration.name)

    link = next((item for item in resource.dedicated_links
                 if item.name == template.dedicated_link), None)
    if len(reduction.terms) > 1 and link is None:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires unavailable dedicated connection "
            f"'{template.dedicated_link}'"
        )
    required_edges = len(reduction.terms) - 1
    capacity = dict(((rid, kind), count)
                    for rid, kind, count in target.dedicated_capacities).get(
        (resource.identity, link.name if link else ""), 0
    )
    if capacity < required_edges:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' dedicated connection requires capacity "
            f"{required_edges}, target provides {capacity}"
        )

    multiplier_a = resource.limit("multiplier_a")
    multiplier_b = resource.limit("multiplier_b")
    product_limit = resource.limit("product")
    accumulator_limit = resource.limit("accumulator")
    evidence: list[str] = []
    nodes: list[ResourceInstance] = []
    previous = "constant:zero"
    for index, term in enumerate(reduction.terms):
        product = term.product_expression
        left, right = product.left, product.right
        if left.type.width <= multiplier_a and right.type.width <= multiplier_b:
            a_value, b_value = left, right
        elif right.type.width <= multiplier_a and left.type.width <= multiplier_b:
            a_value, b_value = right, left
        else:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' product {index} operands "
                f"{left.type.width}x{right.type.width} exceed multiplier ports "
                f"{multiplier_a}x{multiplier_b}"
            )
        if product.type.width > product_limit:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' product {index} width exceeded: "
                f"semantic required width {product.type.width}, resource supports {product_limit}"
            )
        stage_type = (
            product.type if index == 0 else reduction.joins[index - 1].result_type
        )
        if stage_type.width > accumulator_limit:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' accumulator width exceeded at stage {index}: "
                f"semantic required width {stage_type.width}, resource supports {accumulator_limit}"
            )
        mode = (
            "accumulator_minus_product"
            if term.sign is ProductTermSign.SUBTRACT
            else "accumulator_plus_product"
        )
        stage_semantic_identity = (
            term.semantic_identity if index == 0
            else reduction.joins[index - 1].semantic_identity
        )
        stage_expression = (
            product if index == 0 else reduction.joins[index - 1].original_expression
        )
        mappings = (
            SemanticPortMapping("a", _semantic_mapping_identity(a_value), a_value),
            SemanticPortMapping("b", _semantic_mapping_identity(b_value), b_value),
            SemanticPortMapping("pcin", previous),
            SemanticPortMapping("p", stage_semantic_identity, stage_expression),
            SemanticPortMapping("pcout", stage_semantic_identity, stage_expression),
        )
        node_configuration = tuple((
            *configuration.physical_settings,
            ("pipeline_configuration", configuration.name),
            ("accumulator_mode", mode),
            ("term_ordinal", index),
        ))
        nodes.append(ResourceInstance(
            f"product_accumulator{index}", resource.identity, resource.operation,
            node_configuration, mappings,
        ))
        previous = stage_semantic_identity
        evidence.append(
            f"stage {index}: {mode}, product {product.type.width}<={product_limit}, "
            f"accumulator {stage_type.width}<={accumulator_limit}"
        )

    edges = tuple(DedicatedPhysicalEdge(
        f"signed_product_cascade:{index}:{index + 1}", link.name,
        f"product_accumulator{index}", link.source_port,
        f"product_accumulator{index + 1}", link.destination_port,
        link.width, link.placement_relation, link.latency, link.fabric_fallback,
    ) for index in range(required_edges))
    useful_latency = 1 + configuration.latency
    graph = ImplementationGraph(
        semantic_region_identity=reduction.semantic_identity,
        architecture_template_identity=template.identity,
        target_identity=target.identity, target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=tuple(nodes), dedicated_edges=edges,
        latency=useful_latency, initiation_interval=configuration.initiation_interval,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        quantization=conversion, source_origin=reduction.source_origin,
        legality_evidence=(*evidence, "one final FixedConvert remains outside the resource cascade"),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy="auto", target_part=target.part,
        pipeline_configuration_identity=f"{resource.identity}.{configuration.name}",
        active_pipeline_sites=configuration.sites,
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )
    from zlang.target_timing import build_dsp_cascade_timing_dag
    timing_dag = build_dsp_cascade_timing_dag(
        graph, resource, configuration, structural_latency=1,
        output_width=conversion.type.width, exact_latency=exact_latency,
    )
    return replace(graph, latency=timing_dag.output_latency, timing_dag=timing_dag)


def _recognize_symmetric_fir(module: Module, template: ArchitectureTemplate):
    conversions: list[tuple[expr.FixedConvert, Register | None]] = []
    for item in module.next_assignments:
        if isinstance(item.expression, expr.FixedConvert):
            conversions.append((item.expression, item.target if isinstance(item.target, Register) else None))
    for item in module.assignments:
        if isinstance(item.expression, expr.FixedConvert):
            conversions.append((item.expression, None))
    if len(conversions) != 1:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' does not cover semantic region: "
            "expected exactly one final FixedConvert"
        )
    conversion, output_register = conversions[0]
    if not (
        isinstance(conversion.type, FixedType)
        and conversion.type.width == 16 and conversion.type.fraction == 14
        and conversion.rounding is expr.FixedRounding.NEAREST_EVEN
        and conversion.overflow is expr.FixedOverflow.SATURATE
    ):
        raise TargetArchitectureError(
            f"architecture '{template.identity}' requires one final nearest_even/saturating fixed<16,14> quantization"
        )
    terms = _flatten_add(conversion.expression)
    if len(terms) != 8:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' does not cover semantic region: expected eight products, got {len(terms)}"
        )
    grouped: dict[expr.VectorIndex, list[expr.VectorIndex]] = {}
    for term in terms:
        if not isinstance(term, expr.Binary) or term.operator is not expr.BinaryOperator.MULTIPLY:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' does not cover semantic region: reduction term is not a product"
            )
        vector_items = tuple(item for item in (term.left, term.right) if isinstance(item, expr.VectorIndex))
        if len(vector_items) != 2:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' requires vector-indexed sample/coefficient products"
            )
        sample = next((item for item in vector_items if isinstance(item.expression.type, VecType) and item.expression.type.length == 8), None)
        coefficient = next((item for item in vector_items if isinstance(item.expression.type, VecType) and item.expression.type.length == 4), None)
        if sample is None or coefficient is None:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' requires eight samples and four semantically reused coefficients"
            )
        grouped.setdefault(coefficient, []).append(sample)
    if len(grouped) != 4 or any(len(samples) != 2 for samples in grouped.values()):
        raise TargetArchitectureError(
            f"architecture '{template.identity}' cannot prove coefficient symmetry from semantic identities"
        )
    pairs = []
    seen = set()
    for coefficient, samples in sorted(grouped.items(), key=lambda item: item[0].index):
        ordered = tuple(sorted(samples, key=lambda item: item.index))
        if ordered[0].index + ordered[1].index != 7:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' coefficient index {coefficient.index} is not used at mirrored sample taps"
            )
        seen.update(item.index for item in ordered)
        pairs.append((ordered[0], ordered[1], coefficient))
    if seen != set(range(8)):
        raise TargetArchitectureError(f"architecture '{template.identity}' does not cover all sample taps exactly once")
    semantic_latency = _conversion_output_latency(module, conversion, output_register)
    return tuple(pairs), conversion, conversion.expression, output_register, semantic_latency


def _conversion_output_latency(
    module: Module, conversion: expr.FixedConvert, first_register: Register | None,
) -> int:
    if first_register is None:
        if any(item.expression == conversion for item in module.assignments):
            return 0
        raise TargetArchitectureError("fixed conversion is not connected to a module output")
    latency = 1
    current = first_register.name
    visited = {current}
    while True:
        if any(
            isinstance(item.expression, expr.RegisterRef)
            and item.expression.name == current
            for item in module.assignments
        ):
            return latency
        followers = tuple(
            item.target for item in module.next_assignments
            if isinstance(item.target, Register)
            and isinstance(item.expression, expr.RegisterRef)
            and item.expression.name == current
        )
        if len(followers) != 1 or followers[0].name in visited:
            raise TargetArchitectureError(
                "registered fixed conversion must have one deterministic output-delay chain"
            )
        current = followers[0].name
        visited.add(current)
        latency += 1


def _validate_widths(template, resource, pairs, accumulator):
    preadder_limit = resource.limit("preadder")
    b_limit = resource.limit("multiplier_b")
    product_limit = resource.limit("product")
    accumulator_limit = resource.limit("accumulator")
    evidence = []
    for index, (left, right, coefficient) in enumerate(pairs):
        if not isinstance(left.type, FixedType) or not isinstance(right.type, FixedType) or not isinstance(coefficient.type, FixedType):
            raise TargetArchitectureError(f"architecture '{template.identity}' requires signed fixed-point operands")
        if left.type.fraction != right.type.fraction:
            raise TargetArchitectureError(f"architecture '{template.identity}' preadder operands require identical scale")
        preadder_width = max(left.type.width, right.type.width) + 1
        if preadder_width > preadder_limit:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' preadder width exceeded at stage {index}: "
                f"semantic required width {preadder_width}, resource port supports {preadder_limit}"
            )
        if coefficient.type.width > b_limit:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' multiplier input B width exceeded at stage {index}: "
                f"semantic required width {coefficient.type.width}, resource port supports {b_limit}"
            )
        product_width = preadder_width + coefficient.type.width
        if product_width > product_limit:
            raise TargetArchitectureError(
                f"architecture '{template.identity}' multiplication width exceeded at stage {index}: "
                f"semantic required width {product_width}, resource supports {product_limit}"
            )
        evidence.append(f"stage {index}: preadder {preadder_width}<={preadder_limit}, coefficient {coefficient.type.width}<={b_limit}, product {product_width}<={product_limit}")
    if not isinstance(accumulator.type, FixedType):
        raise TargetArchitectureError(f"architecture '{template.identity}' requires a signed fixed-point accumulator")
    if accumulator.type.width > accumulator_limit:
        raise TargetArchitectureError(
            f"architecture '{template.identity}' accumulator width exceeded: semantic required width "
            f"{accumulator.type.width}, resource P/PCIN supports {accumulator_limit}"
        )
    evidence.append(f"accumulator {accumulator.type.width}<={accumulator_limit}")
    evidence.append("one final FixedConvert remains outside the resource cascade")
    return evidence


def _flatten_add(value: expr.Expression) -> tuple[expr.Expression, ...]:
    if isinstance(value, expr.Add):
        return (*_flatten_add(value.left), *_flatten_add(value.right))
    return (value,)


def _expression_identity(value: expr.Expression) -> str:
    return sha256(_semantic_payload(value).encode()).hexdigest()


def _semantic_mapping_identity(value: expr.Expression) -> str:
    if isinstance(value, expr.VectorIndex) and isinstance(value.expression, expr.InputRef):
        return f"port:{value.expression.name}[{value.index}]"
    if isinstance(value, expr.InputRef):
        return f"port:{value.name}"
    return "value:" + _expression_identity(value)


@lru_cache(maxsize=None)
def _semantic_field_names(type_: type) -> tuple[str, ...]:
    return tuple(
        item.name
        for item in fields(type_)
        if item.name not in {"origin", "source_origin"}
    )


def _semantic_payload(value) -> str:
    """Render the historical semantic payload with cached field schemas."""

    if isinstance(value, tuple):
        return "(" + ",".join(_semantic_payload(item) for item in value) + ")"
    if is_dataclass(value):
        body = ",".join(
            f"{name}={_semantic_payload(getattr(value, name))}"
            for name in _semantic_field_names(type(value))
        )
        return f"{type(value).__module__}.{type(value).__name__}({body})"
    return repr(value)


__all__ = [
    "ArchitectureSelectionMode", "TargetArchitectureError",
    "generic_implementation_graph", "load_architecture", "load_architecture_templates", "load_target",
    "map_auto_signed_product_configuration", "map_auto_symmetric_configuration",
    "map_manual_architecture", "select_implementation_graph",
    "validate_clock_requirement", "validate_inventory",
    "validate_memory_configuration", "validate_pipeline_configuration",
]
