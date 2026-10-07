"""Typed predicates over source-defined target resource capabilities.

Target descriptions remain the authority: this module does not infer vendor
properties from a part name or backend.  Candidate providers use these small
predicates to explain why a source-defined architecture is, or is not,
realizable by a resource.  The same predicates apply to FPGA and ASIC target
catalogues because they operate only on :class:`ResourceDefinition` facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from zlang.ir.target import ResourceDefinition


@dataclass(frozen=True)
class CapabilityEvidence:
    """One deterministic source-defined capability evaluation."""

    predicate: str
    actual: str
    satisfied: bool


@dataclass(frozen=True)
class MemoryCapabilityRequest:
    """One exact logical memory shape a target resource must support."""

    width: int
    depth: int
    port_mode: str

    def __post_init__(self) -> None:
        if self.width < 1 or self.depth < 1:
            raise ValueError("memory capability dimensions must be positive")
        if not self.port_mode:
            raise ValueError("memory capability request requires a port mode")


@dataclass(frozen=True)
class MemoryCapabilityAssessment:
    """Source-defined memory capability facts, in validation order."""

    resource_identity: str
    request: MemoryCapabilityRequest
    resource_class_supported: bool
    port_mode_supported: bool
    width_supported: bool
    capacity_supported: bool
    advertised_widths: tuple[int, ...]
    capacity_bits: int

    @property
    def supported(self) -> bool:
        return all((
            self.resource_class_supported,
            self.port_mode_supported,
            self.width_supported,
            self.capacity_supported,
        ))

    def failure_message(self) -> str | None:
        """Return the legacy-stable first rejection, if any."""

        if not self.resource_class_supported:
            return f"resource '{self.resource_identity}' is not memory"
        if not self.port_mode_supported:
            return (
                f"memory resource '{self.resource_identity}' does not support "
                f"port mode '{self.request.port_mode}'"
            )
        if not self.width_supported:
            return (
                f"memory resource '{self.resource_identity}' does not support "
                f"width {self.request.width}"
            )
        if not self.capacity_supported:
            required = self.request.width * self.request.depth
            return (
                f"memory resource '{self.resource_identity}' capacity exceeded: "
                f"{required}>{self.capacity_bits} bits"
            )
        return None


class ResourceCapabilityPredicate(Protocol):
    """A fact a resource must explicitly publish before selection can use it."""

    def evaluate(self, resource: ResourceDefinition) -> CapabilityEvidence:
        """Evaluate against one resource without target-name heuristics."""


def assess_memory_capability(
    resource: ResourceDefinition,
    request: MemoryCapabilityRequest,
) -> MemoryCapabilityAssessment:
    """Assess a bounded memory request using only catalog-published facts."""

    capabilities = dict(resource.capabilities)
    advertised_widths = tuple(sorted(
        int(value) for value in capabilities.get("widths", "").split(".") if value
    ))
    capacity_bits = int(capabilities.get("capacity_bits", "0"))
    return MemoryCapabilityAssessment(
        resource.identity,
        request,
        resource.resource_class in {"block_memory", "distributed_memory"},
        request.port_mode in set(capabilities.get("port_modes", "").split(".")),
        not advertised_widths or request.width in advertised_widths,
        not capacity_bits or request.width * request.depth <= capacity_bits,
        advertised_widths,
        capacity_bits,
    )


@dataclass(frozen=True)
class ResourceClass:
    expected: str

    def evaluate(self, resource: ResourceDefinition) -> CapabilityEvidence:
        return CapabilityEvidence(
            predicate=f"resource_class={self.expected}",
            actual=resource.resource_class,
            satisfied=resource.resource_class == self.expected,
        )


@dataclass(frozen=True)
class DotListCapability:
    """Require one member of a dot-separated source capability value."""

    name: str
    required: str

    def evaluate(self, resource: ResourceDefinition) -> CapabilityEvidence:
        actual = dict(resource.capabilities).get(self.name, "")
        return CapabilityEvidence(
            predicate=f"{self.name} contains {self.required}",
            actual=actual,
            satisfied=self.required in frozenset(filter(None, actual.split("."))),
        )


@dataclass(frozen=True)
class PhysicalBindingRequirement:
    """Require one exact source-declared backend emitter binding."""

    backend: str
    emitter: str
    primitive: str | None

    def evaluate(self, resource: ResourceDefinition) -> CapabilityEvidence:
        expected = ":".join((self.backend, self.emitter, self.primitive or ""))
        actual = tuple(
            ":".join((item.backend, item.emitter, item.primitive or ""))
            for item in resource.physical_bindings
        )
        return CapabilityEvidence(
            predicate=f"physical_binding={expected}",
            actual=",".join(actual),
            satisfied=expected in actual,
        )


def evaluate_capabilities(
    resource: ResourceDefinition,
    predicates: tuple[ResourceCapabilityPredicate, ...],
) -> tuple[CapabilityEvidence, ...]:
    """Evaluate declared requirements in caller-owned deterministic order."""

    return tuple(predicate.evaluate(resource) for predicate in predicates)


def capabilities_satisfied(
    resource: ResourceDefinition,
    predicates: tuple[ResourceCapabilityPredicate, ...],
) -> bool:
    """Return whether every required capability is explicitly present."""

    return all(item.satisfied for item in evaluate_capabilities(resource, predicates))


__all__ = [
    "CapabilityEvidence",
    "DotListCapability",
    "MemoryCapabilityAssessment",
    "MemoryCapabilityRequest",
    "PhysicalBindingRequirement",
    "ResourceCapabilityPredicate",
    "ResourceClass",
    "assess_memory_capability",
    "capabilities_satisfied",
    "evaluate_capabilities",
]
