"""Target-neutral dispatch for bounded implementation-candidate builders.

The planner owns candidate enumeration, costing and selection.  Providers own
only the operation-specific preconditions and construction of one graph from a
source-defined architecture template.  This keeps new target capabilities from
becoming another conditional in :mod:`zlang.target_planner` while preserving
the existing target mappers as the single implementation of each mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from zlang.ir import expressions as expr
from zlang.ir.module import Module
from zlang.ir.target import (
    ArchitectureTemplate,
    ImplementationGraph,
    PipelineConfiguration,
    ResourceDefinition,
    TargetFamilyDefinition,
    TargetInstance,
)
from zlang.target_capabilities import (
    DotListCapability,
    PhysicalBindingRequirement,
    capabilities_satisfied,
)
from zlang.targets import (
    TargetArchitectureError,
    map_auto_multiply_configuration,
    map_auto_multiply_add_configuration,
    map_auto_signed_product_configuration,
    map_auto_symmetric_configuration,
)


@dataclass(frozen=True)
class CandidateRequest:
    """All compiler-owned inputs needed to construct one candidate graph."""

    module: Module
    target: TargetInstance
    family: TargetFamilyDefinition
    resources: tuple[ResourceDefinition, ...]
    template: ArchitectureTemplate
    configuration: PipelineConfiguration
    exact_latency: int | None
    source_expression: expr.Expression | None


class TargetCandidateProvider(Protocol):
    """Build candidates for one source-defined architecture operation."""

    operation: str

    def build(self, request: CandidateRequest) -> ImplementationGraph:
        """Return the graph or raise the canonical target architecture error."""


def _with_natural_latency(
    request: CandidateRequest,
    build: Callable[[int | None], ImplementationGraph],
) -> ImplementationGraph:
    """Preserve the planner's exact-latency reporting behavior.

    An otherwise realizable physical configuration is kept in the candidate
    report when its natural latency exceeds an exact source contract.  The
    central extractor remains the sole owner of whether that candidate is
    legal.  Other mapping errors remain fail-closed.
    """

    try:
        return build(request.exact_latency)
    except ValueError as error:
        if request.exact_latency is None or "exceeds exact latency" not in str(error):
            raise
        return build(None)


@dataclass(frozen=True)
class SignedProductReductionProvider:
    operation: str = "signed_product_reduction"

    def build(self, request: CandidateRequest) -> ImplementationGraph:
        resource = _exact_resource(request)
        if not capabilities_satisfied(
            resource, (DotListCapability("physical_architectures", self.operation),)
        ):
            raise TargetArchitectureError(
                f"resource '{resource.identity}' supports signed accumulator "
                "arithmetic but its physical backend binding does not publish "
                "signed_product_reduction emission"
            )
        return _with_natural_latency(
            request,
            lambda exact_latency: map_auto_signed_product_configuration(
                request.module,
                request.target,
                request.family,
                request.resources,
                request.template,
                request.configuration,
                exact_latency=exact_latency,
                source_expression=request.source_expression,
            ),
        )


@dataclass(frozen=True)
class MultiplyAddProvider:
    operation: str = "multiply_add"

    def build(self, request: CandidateRequest) -> ImplementationGraph:
        resource = _exact_resource(request)
        if not capabilities_satisfied(
            resource,
            (
                PhysicalBindingRequirement(
                    "systemverilog", "dsp48e1_explicit", "DSP48E1"
                ),
            ),
        ):
            raise TargetArchitectureError(
                f"resource '{resource.identity}' has no explicit "
                "DSP48E1 physical binding for multiply_add"
            )
        return _with_natural_latency(
            request,
            lambda exact_latency: map_auto_multiply_add_configuration(
                request.module,
                request.target,
                request.family,
                request.resources,
                request.template,
                request.configuration,
                exact_latency=exact_latency,
                source_expression=request.source_expression,
            ),
        )


@dataclass(frozen=True)
class MultiplyProvider:
    """Build one physical full-width multiply candidate from catalog facts."""

    operation: str = "multiply"

    def build(self, request: CandidateRequest) -> ImplementationGraph:
        resource = _exact_resource(request)
        if not capabilities_satisfied(
            resource,
            (
                PhysicalBindingRequirement(
                    "systemverilog", "dsp48e1_explicit", "DSP48E1"
                ),
            ),
        ):
            raise TargetArchitectureError(
                f"resource '{resource.identity}' has no explicit DSP48E1 "
                "physical binding for multiply"
            )
        return _with_natural_latency(
            request,
            lambda exact_latency: map_auto_multiply_configuration(
                request.module,
                request.target,
                request.family,
                request.resources,
                request.template,
                request.configuration,
                exact_latency=exact_latency,
                source_expression=request.source_expression,
            ),
        )


@dataclass(frozen=True)
class SymmetricFirCascadeProvider:
    operation: str = "symmetric_fir_cascade"

    def build(self, request: CandidateRequest) -> ImplementationGraph:
        return _with_natural_latency(
            request,
            lambda exact_latency: map_auto_symmetric_configuration(
                request.module,
                request.target,
                request.family,
                request.resources,
                request.template,
                request.configuration,
                exact_latency=exact_latency,
            ),
        )


def _exact_resource(request: CandidateRequest) -> ResourceDefinition:
    matches = tuple(
        item for item in request.resources if item.name == request.template.resource_name
    )
    if len(matches) != 1:
        raise TargetArchitectureError(
            f"resource '{request.template.resource_name}' unavailable"
        )
    return matches[0]


_PROVIDERS: tuple[TargetCandidateProvider, ...] = (
    SignedProductReductionProvider(),
    MultiplyProvider(),
    MultiplyAddProvider(),
    SymmetricFirCascadeProvider(),
)
_PROVIDERS_BY_OPERATION = {provider.operation: provider for provider in _PROVIDERS}


def candidate_provider_for(operation: str) -> TargetCandidateProvider:
    """Return the sole provider for a declared architecture operation."""

    try:
        return _PROVIDERS_BY_OPERATION[operation]
    except KeyError as error:
        raise TargetArchitectureError(
            f"unsupported target architecture operation '{operation}'"
        ) from error


__all__ = [
    "CandidateRequest",
    "TargetCandidateProvider",
    "candidate_provider_for",
]
