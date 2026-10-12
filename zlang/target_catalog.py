"""Source-defined target/resource loading and validation."""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from zlang.ast import nodes as ast
from zlang.ir import expressions as expr
from zlang.ir.target import (
    ArchitectureTemplate,
    PhysicalBinding,
    PipelineConfiguration,
    PipelineSite,
    ResourceDedicatedLink,
    ResourceDefinition,
    ResourcePort,
    ResourceRegisterSite,
    TargetFamilyDefinition,
    TargetInstance,
)
from zlang.ir.types import FixedType, SIntType, UFixedType, UIntType
from zlang.stdlib import available_stdlib_modules, resolve_stdlib
from zlang.target_capabilities import (
    MemoryCapabilityRequest,
    assess_memory_capability,
)


class ArchitectureSelectionMode(str, Enum):
    GENERIC = "generic"
    PREFERRED = "preferred"
    REQUIRED = "required"


class TargetArchitectureError(ValueError):
    """A source-defined target or manually requested architecture is illegal."""


_DSP_NUMERIC_TYPES = (FixedType, UFixedType, SIntType, UIntType)


def _dsp_signed_width(type_) -> int:
    """Physical signed width needed to preserve one exact numeric value."""

    return type_.width + int(isinstance(type_, (UIntType, UFixedType)))


def _dsp_value_expression(
    source_expression: expr.Expression,
) -> tuple[expr.Expression, expr.Expression, str]:
    """Return arithmetic value, public boundary, and boundary description."""

    if isinstance(source_expression, expr.FixedConvert):
        return (
            source_expression.expression,
            source_expression,
            "one final FixedConvert remains outside the resource cascade",
        )
    if isinstance(source_expression.type, _DSP_NUMERIC_TYPES):
        return (
            source_expression,
            source_expression,
            "exact numeric projection remains outside the resource cascade",
        )
    raise TargetArchitectureError(
        "DSP arithmetic covering requires a signed/unsigned integer or fixed-point result"
    )


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
    assessment = assess_memory_capability(
        resource, MemoryCapabilityRequest(width, depth, port_mode)
    )
    if (failure := assessment.failure_message()) is not None:
        raise TargetArchitectureError(failure)


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
