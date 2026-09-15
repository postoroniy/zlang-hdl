"""Target-neutral resource matching for typed scalar operations.

The scheduler asks matchers whether an already typed operation fits a
source-described resource.  Matchers do not schedule stages or alter value IR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from zlang.ir import expressions as expr
from zlang.ir.physical_types import signed_arithmetic_port_width
from zlang.ir.target import ResourceDefinition


@dataclass(frozen=True)
class ResourceMatch:
    resource_identity: str
    resource_name: str
    resource_class: str
    mapping: str
    estimated_delay_ps: int
    estimated_lut: int
    estimated_dsp: int
    pipeline_options: tuple[str, ...]
    constraints: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not self.resource_identity or not self.resource_name or not self.mapping:
            raise ValueError("resource match identities must not be empty")
        if self.estimated_delay_ps <= 0:
            raise ValueError("resource match needs a positive target delay estimate")
        if self.estimated_lut < 0 or self.estimated_dsp < 0:
            raise ValueError("resource match costs must not be negative")


class ResourceMatcher(Protocol):
    """One provider for a source-described target resource family."""

    def match(
        self,
        operation: expr.Expression,
        operation_class: str,
        resource: ResourceDefinition,
    ) -> ResourceMatch | None: ...


@dataclass(frozen=True)
class DspMultiplyResourceMatcher:
    """Matcher for the accepted source-described DSP multiply capability.

    No vendor primitive appears here.  DSP48E1 is selected later through the
    resource's physical binding; another target may publish the same semantic
    resource contract and reuse this matcher.
    """

    def match(
        self,
        operation: expr.Expression,
        operation_class: str,
        resource: ResourceDefinition,
    ) -> ResourceMatch | None:
        if operation_class not in {"multiply", "fixed_multiply"}:
            return None
        operands = _operation_children(operation)
        if resource.resource_class != "dsp_mac" or len(operands) != 2:
            return None
        try:
            fits = (
                signed_arithmetic_port_width(operands[0].type)
                <= resource.limit("multiplier_a")
                and signed_arithmetic_port_width(operands[1].type)
                <= resource.limit("multiplier_b")
            ) or (
                signed_arithmetic_port_width(operands[1].type)
                <= resource.limit("multiplier_a")
                and signed_arithmetic_port_width(operands[0].type)
                <= resource.limit("multiplier_b")
            )
        except ValueError:
            return None
        site = next(
            (
                item
                for item in resource.pipeline_sites
                if item.semantic_location == "multiply"
                and item.estimated_delay_ps > 0
            ),
            None,
        )
        if not fits or site is None:
            return None
        return ResourceMatch(
            resource.identity,
            resource.name,
            resource.resource_class,
            "scalar_multiply",
            site.estimated_delay_ps,
            0,
            1,
            tuple(
                item.name
                for item in resource.pipeline_configurations
                if item.initiation_interval == 1
            ),
            tuple(sorted(resource.limits)),
        )


def _operation_children(operation: expr.Expression) -> tuple[expr.Expression, ...]:
    if isinstance(operation, expr.Binary):
        return operation.left, operation.right
    return ()


__all__ = ["DspMultiplyResourceMatcher", "ResourceMatch", "ResourceMatcher"]
