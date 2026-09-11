"""One backend-independent scheduled value graph shared by planning layers.

The semantic expression remains the value definition.  This record describes
where its pure operations execute and where exact visible-cycle boundaries are
implemented.  Generic and target resource planners attach different physical
bindings to the same typed operation identities instead of constructing an
unrelated second timing model.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


SCHEDULED_VALUE_GRAPH_SCHEMA = "zlang-scheduled-value-graph-v2"


@dataclass(frozen=True)
class ScheduledValueResourceBinding:
    operation_identity: str
    resource_instance_identity: str
    resource_definition_identity: str
    pipeline_configuration_identity: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_identity:
            raise ValueError("scheduled resource binding requires an operation")
        if not self.resource_instance_identity or not self.resource_definition_identity:
            raise ValueError("scheduled resource binding requires exact resource identities")


@dataclass(frozen=True)
class ScheduledValueGraph:
    source_expression_identity: str
    selected_value_identity: str
    operation_identities: tuple[str, ...]
    operation_semantic_identities: tuple[tuple[str, str], ...]
    dependencies: tuple[tuple[str, str, int], ...]
    stage_assignment: tuple[tuple[str, int], ...]
    stage_delays_ps: tuple[int, ...]
    exact_latency: int
    initiation_interval: int
    cost_source: str
    resource_bindings: tuple[ScheduledValueResourceBinding, ...] = ()
    cut_identities: tuple[str, ...] = ()
    alignment_delay_identities: tuple[str, ...] = ()
    compensation_delay_identities: tuple[str, ...] = ()
    rewrite_certificate: tuple[str, ...] = ()
    schema: str = SCHEDULED_VALUE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEDULED_VALUE_GRAPH_SCHEMA:
            raise ValueError("unsupported scheduled value graph schema")
        if not self.source_expression_identity or not self.selected_value_identity:
            raise ValueError("scheduled value graph requires source and selected identities")
        if self.exact_latency < 1 or self.initiation_interval < 1:
            raise ValueError("scheduled value graph requires positive latency and II")
        if len(self.stage_delays_ps) != self.exact_latency:
            raise ValueError("scheduled value graph must describe every visible stage")
        if len(self.operation_identities) != len(set(self.operation_identities)):
            raise ValueError("scheduled value graph operation identities must be unique")
        known = set(self.operation_identities)
        if len(self.operation_semantic_identities) != len(known):
            raise ValueError("scheduled value graph repeats operation semantic metadata")
        if len(self.stage_assignment) != len(known):
            raise ValueError("scheduled value graph repeats stage assignment metadata")
        semantic = dict(self.operation_semantic_identities)
        stages = dict(self.stage_assignment)
        if set(semantic) != known or set(stages) != known:
            raise ValueError("scheduled value graph operation metadata is incomplete")
        if any(stage < 0 or stage >= self.exact_latency for stage in stages.values()):
            raise ValueError("scheduled value operation is outside the exact latency")
        if any(delay < 0 for delay in self.stage_delays_ps):
            raise ValueError("scheduled stage delay cannot be negative")
        if any(cycles < 0 for _, _, cycles in self.dependencies):
            raise ValueError("scheduled dependency latency cannot be negative")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("scheduled value graph repeats a dependency")
        if any(source == destination for source, destination, _ in self.dependencies):
            raise ValueError("scheduled operation cannot depend on itself")
        destinations = {destination for _, destination, _ in self.dependencies}
        if not destinations <= known:
            raise ValueError("scheduled dependency references an unknown destination")
        for source, destination, cycles in self.dependencies:
            if source not in known:
                continue
            if stages[source] > stages[destination]:
                raise ValueError("scheduled dependency consumes a future-stage operation")
            if cycles != stages[destination] - stages[source]:
                raise ValueError("scheduled dependency latency disagrees with stage assignment")
        # Stage monotonicity alone does not reject a combinational cycle when
        # every member is assigned to the same stage.  Keep the serialized
        # physical graph independently valid instead of relying on the source
        # expression walker having produced a DAG.
        successors: dict[str, list[str]] = {identity: [] for identity in known}
        indegree = {identity: 0 for identity in known}
        for source, destination, _ in self.dependencies:
            if source not in known:
                continue
            successors[source].append(destination)
            indegree[destination] += 1
        ready = sorted(identity for identity, degree in indegree.items() if degree == 0)
        visited = 0
        while ready:
            source = ready.pop(0)
            visited += 1
            for destination in sorted(successors[source]):
                indegree[destination] -= 1
                if indegree[destination] == 0:
                    ready.append(destination)
                    ready.sort()
        if visited != len(known):
            raise ValueError("scheduled value graph contains an operation cycle")
        if any(item.operation_identity not in known for item in self.resource_bindings):
            raise ValueError("resource binding references an unknown scheduled operation")
        bound_operations = tuple(
            item.operation_identity for item in self.resource_bindings
        )
        if len(bound_operations) != len(set(bound_operations)):
            raise ValueError("scheduled operation has more than one resource binding")

    @property
    def identity(self) -> str:
        return sha256(repr((self.schema, self)).encode("utf-8")).hexdigest()


__all__ = [
    "SCHEDULED_VALUE_GRAPH_SCHEMA",
    "ScheduledValueGraph",
    "ScheduledValueResourceBinding",
]
