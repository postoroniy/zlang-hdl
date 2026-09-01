"""Stable semantic identities for profile-addressable implementation regions.

The first profile slice deliberately addresses only public scalar wire-output
assignments in one already-typed module specialization.  Region identities are
derived from semantic IR; source spans, filesystem paths, generated names, and
backend RTL names never participate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from zlang.common import stable_digest, stable_json
from zlang.diagnostics import DiagnosticError
from zlang.ir.expressions import Expression
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Assignment, Module, Port, PortDirection
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.types import HardwareType, StructType, TupleType, VecType
from zlang.ir.type_codec import canonical_type_data


IMPLEMENTATION_REGION_SCHEMA = "zlang-implementation-region-v1"


class ImplementationRegionError(DiagnosticError):
    """A profile region selector cannot be resolved exactly."""

    default_code = "ZL-IMPL-REGION"


@dataclass(frozen=True)
class ImplementationRegion:
    """One typed public scalar implementation region.

    ``expression`` is retained for planning but is excluded from dataclass
    equality and never serialized by the identity machinery.  Its stable typed
    semantic identity is carried explicitly instead.
    """

    identity: str
    module_identity: str
    output_binding_identity: str
    expression_identity: str
    canonical_result_type: str
    module_name: str
    output_name: str
    expression: Expression = field(compare=False, repr=False)

    @property
    def result_type(self) -> HardwareType:
        return self.expression.type

    def to_data(self) -> dict[str, str]:
        return {
            "identity": self.identity,
            "module_identity": self.module_identity,
            "output_binding_identity": self.output_binding_identity,
            "expression_identity": self.expression_identity,
            "canonical_result_type": self.canonical_result_type,
            "module_name": self.module_name,
            "output_name": self.output_name,
        }


def module_semantic_identity(module: Module) -> str:
    """Identify a logical module specialization without source/physical data."""

    return stable_digest(
        {
            "schema": "zlang-module-semantic-specialization-v1",
            "module": module.name,
            "parameters": [
                {"name": name, "kind": kind, "value": value}
                for name, kind, value in module.parameters
            ],
        }
    )


def output_binding_identity(module_identity: str, output: Port) -> str:
    """Identify one public typed output binding in a module specialization."""

    return stable_digest(
        {
            "schema": "zlang-public-output-binding-v1",
            "module_identity": module_identity,
            "direction": output.direction.value,
            "name": output.name,
            "protocol": output.protocol.value,
            "type": canonical_type_data(output.type),
        }
    )


def discover_implementation_regions(
    module: Module,
    *,
    recursive: bool = False,
) -> tuple[ImplementationRegion, ...]:
    """Discover profile-addressable regions in one typed specialization.

    Recursive hierarchy selection is intentionally outside the first profile
    slice.  Callers must select a concrete typed child specialization rather
    than accidentally treating a physical instance path as semantic identity.
    """

    if recursive:
        raise ImplementationRegionError(
            "recursive implementation-region discovery is not supported; "
            "select a concrete module specialization"
        )

    module_identity = module_semantic_identity(module)
    regions: list[ImplementationRegion] = []
    bindings: dict[str, ImplementationRegion] = {}
    identities: dict[str, ImplementationRegion] = {}
    for assignment in module.assignments:
        if not _is_public_scalar_wire_assignment(assignment):
            continue
        output = assignment.target
        assert isinstance(output, Port)
        if output.type != assignment.expression.type:
            raise ImplementationRegionError(
                f"typed implementation region '{module.name}.{output.name}' "
                "has mismatched output and expression types"
            )
        binding_identity = output_binding_identity(module_identity, output)
        expression_identity = expression_semantic_identity(assignment.expression)
        canonical_result_type = stable_json(canonical_type_data(assignment.expression.type))
        identity = stable_digest(
            {
                "schema": IMPLEMENTATION_REGION_SCHEMA,
                "module_identity": module_identity,
                "output_binding_identity": binding_identity,
                "expression_identity": expression_identity,
                "result_type": canonical_type_data(assignment.expression.type),
            }
        )
        region = ImplementationRegion(
            identity=identity,
            module_identity=module_identity,
            output_binding_identity=binding_identity,
            expression_identity=expression_identity,
            canonical_result_type=canonical_result_type,
            module_name=module.name,
            output_name=output.name,
            expression=assignment.expression,
        )
        previous = bindings.get(binding_identity)
        if previous is not None:
            raise ImplementationRegionError(
                f"duplicate implementation region binding '{module.name}.{output.name}'"
            )
        collision = identities.get(identity)
        if collision is not None:
            raise ImplementationRegionError(
                f"duplicate discovered implementation region identity '{identity}'"
            )
        bindings[binding_identity] = region
        identities[identity] = region
        regions.append(region)
    return tuple(sorted(regions, key=lambda item: (item.output_name, item.identity)))


def match_implementation_regions(
    regions_or_module: Iterable[ImplementationRegion] | Module,
    requested_identities: Iterable[str],
) -> tuple[ImplementationRegion, ...]:
    """Resolve exact profile selectors and reject duplicate or stale entries."""

    regions = (
        discover_implementation_regions(regions_or_module)
        if isinstance(regions_or_module, Module)
        else tuple(regions_or_module)
    )
    by_identity: dict[str, ImplementationRegion] = {}
    for region in regions:
        if region.identity in by_identity:
            raise ImplementationRegionError(
                f"duplicate discovered implementation region identity '{region.identity}'"
            )
        by_identity[region.identity] = region

    selected: list[ImplementationRegion] = []
    requested: set[str] = set()
    for identity in requested_identities:
        if identity in requested:
            raise ImplementationRegionError(
                f"duplicate implementation region selector '{identity}'"
            )
        requested.add(identity)
        region = by_identity.get(identity)
        if region is None:
            raise ImplementationRegionError(
                f"unknown or stale implementation region identity '{identity}'"
            )
        selected.append(region)
    return tuple(selected)


def _is_public_scalar_wire_assignment(assignment: Assignment) -> bool:
    target = assignment.target
    return (
        isinstance(target, Port)
        and target.direction is PortDirection.OUTPUT
        and target.protocol is InterfaceProtocol.WIRE
        and assignment.signal is None
        and assignment.channel is None
        and not isinstance(target.type, (StructType, TupleType, VecType))
    )


__all__ = [
    "IMPLEMENTATION_REGION_SCHEMA",
    "ImplementationRegion",
    "ImplementationRegionError",
    "canonical_type_data",
    "discover_implementation_regions",
    "match_implementation_regions",
    "module_semantic_identity",
    "output_binding_identity",
]
