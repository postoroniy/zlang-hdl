# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Recursive verification requirement ownership and scope planning."""

from __future__ import annotations

from dataclasses import dataclass, replace

from zlang.compilation_products import CompilationResult
from zlang.common import stable_digest
from zlang.ir import formal as ir_formal
from zlang.ir import formal_ownership
from zlang.ir.formal_observations import recursive_observation_id
from zlang.ir import formal_predicates
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.ir.interfaces import ReadyValidSignal
from zlang.ir.types import StructType, TupleType, VecType


@dataclass(frozen=True)
class _RecursiveRequirementClosure:
    """Exact implementation-owned RV dependency cone for one child scope.

    ``external`` requirements remain ordinary harness assumptions.  Each
    ``dependency`` pairs an internal automatic requirement with the existing
    source-endpoint guarantee that must be checked in the same root harness.
    Anything that cannot be expressed by that narrow relationship remains a
    fail-closed blocker.
    """

    external: tuple[object, ...]
    blockers: tuple[tuple[object, str], ...]
    dependencies: tuple[tuple[object, object], ...]


@dataclass(frozen=True)
class _RecursiveCoverGoal:
    """One existing feasibility predicate instantiated at a physical path."""

    concrete_property_id: str
    instance_identity: str
    physical_instance_path: tuple[str, ...]
    property: ir_formal.CoverProperty


@dataclass
class _RecursiveScopePublication:
    """Mutable publication accumulator; emitted deterministically at the end."""

    scope_id: str
    name: str
    clock: str
    reset: str
    physical_instance_path: tuple[str, ...]
    requirements: dict[str, dict[str, object]]
    goals: dict[str, dict[str, object]]
    source_origin: object | None


def _conjoin_predicates(
    values: tuple[formal_predicates.FormalPredicate, ...],
) -> formal_predicates.FormalPredicate:
    if not values:
        raise ir_formal.FormalError(
            "a recursive feasibility cover requires a predicate"
        )
    result = values[0]
    for value in values[1:]:
        result = formal_predicates.Binary(
            formal_predicates.FormalBinaryOperator.LOGICAL_AND,
            result,
            value,
            1,
            formal_predicates.FormalSignedness.BIT,
        )
    return result


def _guard_recursive_property(
    concrete: object,
    requirements: tuple[object, ...],
) -> object:
    """Guard one recursive assertion without turning requirements into assumes."""

    if not requirements:
        return concrete
    predicate = concrete.property.predicate
    requirement_predicates = tuple(
        item.property.predicate for item in requirements
        if item.property.predicate is not None
    )
    if predicate is None or len(requirement_predicates) != len(requirements):
        return concrete
    guard = _conjoin_predicates(requirement_predicates)
    guarded = formal_predicates.Binary(
        formal_predicates.FormalBinaryOperator.IMPLIES,
        guard,
        predicate,
        1,
        formal_predicates.FormalSignedness.BIT,
    )
    return replace(
        concrete,
        property=replace(
            concrete.property,
            expression=f"({guard.render()}) -> ({predicate.render()})",
            predicate=guarded,
            relevant_signals=guarded.observation_ids(),
        ),
    )


def _controlled_assumption_ids(concrete: object, module: object) -> tuple[str, ...]:
    """Return only the leaves whose driver an assumption may constrain.

    Automatic protocol predicates legitimately *observe* implementation-owned
    condition signals (for example ready) while constraining only the source's
    valid/payload behavior.  User-authored assumptions have already passed the
    semantic ownership check, so every non-clock/reset observation is a
    controlled leaf and must reach a real root environment input.
    """

    generated = concrete.property.generated_from or ""
    refs = tuple(
        item.local_semantic_id
        for item in concrete.object_refs
        if item.local_semantic_id not in {"clock", "reset"}
    )
    if generated.startswith("ready_valid:"):
        endpoint = generated.removeprefix("ready_valid:")
        return tuple(
            item for item in refs
            if item in {
                f"port:{endpoint}.{ReadyValidSignal.VALID.value}",
                f"port:{endpoint}.{ReadyValidSignal.PAYLOAD.value}",
            }
        )
    if generated.startswith("credit:"):
        endpoint = generated.split(":", 2)[1]
        port = next(
            (item for item in module.ports if item.name == endpoint), None
        )
        if port is None:
            return refs
        controlled = (
            {"payload", "send"}
            if port.direction.value == "input"
            else {"return"}
        )
        return tuple(
            item for item in refs
            if item.removeprefix(f"port:{endpoint}.") in controlled
        )
    return refs

class _RecursiveRequirementPlanner:
    """Own recursive requirement classification, closure, and scope state."""

    def __init__(
        self,
        result: CompilationResult,
        source_safety: tuple[object, ...],
        recursive_properties: tuple[object, ...],
    ) -> None:
        self.result = result
        self.root_path = (result.ir.name,)
        self.properties = recursive_properties
        self.safety = tuple(
            item
            for item in recursive_properties
            if item.property.kind is ir_formal.PropertyKind.ASSERTION
        )
        self.assumptions_by_instance = {
            identity: tuple(
                item
                for item in recursive_properties
                if item.instance_identity == identity
                and item.property.kind is ir_formal.PropertyKind.ASSUMPTION
            )
            for identity in sorted(
                {item.instance_identity for item in recursive_properties}
            )
        }
        recursive_design = result.recursive_formal_design
        self.nodes = {
            item.instance_identity: item
            for item in getattr(recursive_design, "instances", ())
        }
        self.hierarchy = build_hierarchy_index(result.ir)
        self.scopes: dict[str, _RecursiveScopePublication] = {}
        self._local_scope_cache: dict[
            tuple[tuple[str, ...], str],
            tuple[
                tuple[object, ...],
                formal_predicates.FormalPredicate | None,
                str | None,
            ],
        ] = {}
        self._property_by_id = {
            item.concrete_property_id: item for item in recursive_properties
        }
        self._dependency_cache: dict[str, _RecursiveRequirementClosure] = {}
        self._external_by_instance: dict[str, tuple[object, ...]] = {}
        self._blockers_by_instance: dict[
            str, tuple[tuple[object, str], ...]
        ] = {}
        self._discharged_by_instance: dict[
            str, tuple[tuple[object, str], ...]
        ] = {}
        self._classify_assumptions(source_safety)

    def _classify_assumptions(self, source_safety: tuple[object, ...]) -> None:
        guarantee_by_endpoint: dict[tuple[tuple[str, ...], str], str] = {}
        for item in source_safety:
            generated = item.generated_from or ""
            if generated.startswith("ready_valid:"):
                guarantee_by_endpoint[
                    (self.root_path, generated.removeprefix("ready_valid:"))
                ] = item.id
        for item in self.safety:
            generated = item.property.generated_from or ""
            if generated.startswith("ready_valid:"):
                guarantee_by_endpoint[
                    (
                        item.physical_instance_path,
                        generated.removeprefix("ready_valid:"),
                    )
                ] = item.concrete_property_id

        for instance_identity, assumptions in self.assumptions_by_instance.items():
            node = self.nodes[instance_identity]
            entry = self.hierarchy.at(node.physical_instance_path)
            external: list[object] = []
            blockers: list[tuple[object, str]] = []
            discharged: list[tuple[object, str]] = []
            for assumption in assumptions:
                controlled = _controlled_assumption_ids(assumption, entry.module)
                if not controlled:
                    external.append(assumption)
                    continue
                automatic = (assumption.property.generated_from or "").startswith(
                    ("ready_valid:", "credit:")
                )
                ownership = formal_ownership.resolve_recursive_assumption_ownership(
                    self.result.ir,
                    assumption.physical_instance_path,
                    controlled,
                    automatic_protocol=automatic,
                    hierarchy=self.hierarchy,
                )
                if (
                    ownership.disposition
                    is formal_ownership.RecursiveAssumptionDisposition.EXTERNAL
                ):
                    external.append(assumption)
                    continue
                if (
                    ownership.disposition
                    is formal_ownership.RecursiveAssumptionDisposition.INTERNAL_GUARANTEED
                ):
                    guarantee_id = guarantee_by_endpoint.get(
                        (
                            ownership.guarantee_path or (),
                            ownership.guarantee_port or "",
                        )
                    )
                    if guarantee_id is not None:
                        discharged.append((assumption, guarantee_id))
                        continue
                    blockers.append(
                        (
                            assumption,
                            "internally driven automatic requirement has no published "
                            "exact safety verification source-endpoint guarantee",
                        )
                    )
                    continue
                blockers.append(
                    (
                        assumption,
                        ownership.reason
                        or "recursive assumption ownership is unresolved",
                    )
                )
            self._external_by_instance[instance_identity] = tuple(external)
            self._blockers_by_instance[instance_identity] = tuple(blockers)
            self._discharged_by_instance[instance_identity] = tuple(discharged)

    def local_requirements(
        self,
        concrete: object,
    ) -> tuple[
        tuple[object, ...],
        formal_predicates.FormalPredicate | None,
        str | None,
    ]:
        generated = concrete.property.generated_from or ""
        parts = generated.split(":")
        if (
            len(parts) < 3
            or parts[0] not in {"verification-assert", "verification-ensure"}
            or parts[1] == "$module"
        ):
            return (), None, None
        scope_name = parts[1]
        key = (concrete.physical_instance_path, scope_name)
        cached = self._local_scope_cache.get(key)
        if cached is not None:
            return cached
        entry = self.hierarchy.at(concrete.physical_instance_path)
        scope = next(
            (
                item
                for item in entry.module.verification_scopes
                if item.name == scope_name
            ),
            None,
        )
        if scope is None or not scope.requirements:
            result_ = ((), None, None)
            self._local_scope_cache[key] = result_
            return result_
        source_cover = next(
            (
                item
                for item in ir_formal.generate_properties(entry.module).covers
                if item.generated_from == f"verification-feasibility:{scope_name}"
            ),
            None,
        )
        if source_cover is None or source_cover.predicate is None:
            result_ = (
                tuple(scope.requirements),
                None,
                "recursive contract requirements have no structured feasibility predicate",
            )
            self._local_scope_cache[key] = result_
            return result_
        controlled = tuple(
            item
            for item in source_cover.predicate.observation_ids()
            if item not in {"clock", "reset"}
        )
        blocker: str | None = None
        if controlled:
            ownership = formal_ownership.resolve_recursive_assumption_ownership(
                self.result.ir,
                concrete.physical_instance_path,
                controlled,
                automatic_protocol=False,
                hierarchy=self.hierarchy,
            )
            if (
                ownership.disposition
                is not formal_ownership.RecursiveAssumptionDisposition.EXTERNAL
            ):
                blocker = (
                    "user-authored recursive requirement is not controlled by "
                    "the root environment: "
                    + (ownership.reason or "ownership is unresolved")
                )
        mapped = formal_predicates.map_observations(
            source_cover.predicate,
            lambda observation: formal_predicates.ObservationRef(
                recursive_observation_id(
                    concrete.instance_identity,
                    observation.semantic_signal_id,
                ),
                observation.width,
                observation.signedness,
                observation.cycle,
            ),
        )
        result_ = (tuple(scope.requirements), mapped, blocker)
        self._local_scope_cache[key] = result_
        return result_

    @staticmethod
    def _unique(items: list[object]) -> tuple[object, ...]:
        by_id: dict[str, object] = {}
        for item in items:
            by_id.setdefault(item.concrete_property_id, item)
        return tuple(by_id[key] for key in sorted(by_id))

    def requirement_closure(
        self,
        instance_identity: str,
        stack: tuple[str, ...] = (),
    ) -> _RecursiveRequirementClosure:
        cached = self._dependency_cache.get(instance_identity)
        if cached is not None:
            return cached
        if instance_identity in stack:
            assumption = next(
                iter(self.assumptions_by_instance.get(instance_identity, ())),
                None,
            )
            if assumption is None:
                raise ir_formal.FormalError(
                    "recursive ready/valid requirement dependency cycle has no "
                    "source assumption"
                )
            return _RecursiveRequirementClosure(
                (),
                ((assumption, "recursive ready/valid requirement dependency cycle"),),
                (),
            )

        external = list(self._external_by_instance.get(instance_identity, ()))
        blockers = list(self._blockers_by_instance.get(instance_identity, ()))
        dependencies: list[tuple[object, object]] = []
        for requirement, guarantee_id in self._discharged_by_instance.get(
            instance_identity, ()
        ):
            if (
                requirement.property.predicate is None
                or requirement.property.non_executable_reason is not None
            ):
                blockers.append(
                    (
                        requirement,
                        requirement.property.non_executable_reason
                        or "internally driven automatic ready/valid requirement "
                        "has no structured predicate",
                    )
                )
                continue
            guarantee = self._property_by_id.get(guarantee_id)
            if guarantee is None:
                blockers.append(
                    (
                        requirement,
                        "internally driven automatic requirement references an "
                        f"unavailable source guarantee '{guarantee_id}'",
                    )
                )
                continue
            if (
                guarantee.property.predicate is None
                or guarantee.property.non_executable_reason is not None
            ):
                blockers.append(
                    (
                        requirement,
                        guarantee.property.non_executable_reason
                        or "source-endpoint ready/valid guarantee has no structured "
                        "predicate",
                    )
                )
                continue
            requirement_node = self.nodes[instance_identity]
            guarantee_node = self.nodes[guarantee.instance_identity]
            if (
                requirement_node.clock_domain != guarantee_node.clock_domain
                or requirement_node.reset_domain != guarantee_node.reset_domain
            ):
                blockers.append(
                    (
                        requirement,
                        "internally driven automatic ready/valid requirement crosses "
                        "a clock/reset domain",
                    )
                )
                continue
            upstream = self.requirement_closure(
                guarantee.instance_identity,
                (*stack, instance_identity),
            )
            external.extend(upstream.external)
            blockers.extend(upstream.blockers)
            dependencies.extend(upstream.dependencies)
            dependencies.append((requirement, guarantee))

        unique_blockers: dict[str, tuple[object, str]] = {}
        for item, reason in blockers:
            unique_blockers.setdefault(item.concrete_property_id, (item, reason))
        unique_dependencies: dict[str, tuple[object, object]] = {}
        for requirement, guarantee in dependencies:
            unique_dependencies.setdefault(
                requirement.concrete_property_id,
                (requirement, guarantee),
            )
        closure = _RecursiveRequirementClosure(
            self._unique(external),
            tuple(unique_blockers[key] for key in sorted(unique_blockers)),
            tuple(
                unique_dependencies[key]
                for key in sorted(unique_dependencies)
            ),
        )
        self._dependency_cache[instance_identity] = closure
        return closure

    def supporting_assertions(
        self,
        closure: _RecursiveRequirementClosure,
    ) -> tuple[object, ...]:
        result_: list[object] = []
        seen: set[str] = set()
        for _, guarantee in closure.dependencies:
            if guarantee.concrete_property_id in seen:
                continue
            immediate_requirements = tuple(
                item
                for item, _ in self._discharged_by_instance.get(
                    guarantee.instance_identity, ()
                )
            )
            result_.append(
                _guard_recursive_property(guarantee, immediate_requirements)
            )
            seen.add(guarantee.concrete_property_id)
        return tuple(result_)

    @staticmethod
    def _scoped_requirement_id(scope_id: str, source_id: str) -> str:
        return f"{scope_id}.requirement." + stable_digest(source_id, length=16)

    @staticmethod
    def _requirement_record(
        identifier: str,
        name: str,
        source_origin: object | None,
    ) -> dict[str, object]:
        return {
            "id": identifier,
            "name": name,
            "source_origin": (
                None if source_origin is None else source_origin.to_data()
            ),
        }

    def scope_context(
        self,
        concrete: object,
        *,
        clock_domain: str,
        reset_domain: str,
    ) -> tuple[
        str,
        tuple[object, ...],
        tuple[str, ...],
        formal_predicates.FormalPredicate | None,
        str | None,
        tuple[object, ...],
        tuple[object, ...],
    ]:
        closure = self.requirement_closure(concrete.instance_identity)
        external = closure.external
        blockers = closure.blockers
        dependencies = closure.dependencies
        immediate_requirements = self.immediate_requirements(
            concrete.instance_identity
        )
        supporting_assertions = self.supporting_assertions(closure)
        local_requirements, local_predicate, local_blocker = (
            self.local_requirements(concrete)
        )
        scope_key = {
            "path": list(concrete.physical_instance_path),
            "external": [item.concrete_property_id for item in external],
            "blocked": [item.concrete_property_id for item, _ in blockers],
            "internal_dependencies": [
                [
                    requirement.concrete_property_id,
                    guarantee.concrete_property_id,
                ]
                for requirement, guarantee in dependencies
            ],
            "local": [item.semantic_id for item in local_requirements],
            "clock": clock_domain,
            "reset": reset_domain,
        }
        scope_id = (
            f"recursive-scope:{concrete.instance_identity}"
            if not external
            and not blockers
            and not dependencies
            and not local_requirements
            else "recursive-scope:" + stable_digest(scope_key, length=24)
        )

        scoped_external_items: list[object] = []
        for item in external:
            entry = self.hierarchy.at(item.physical_instance_path)
            automatic = (item.property.generated_from or "").startswith(
                ("ready_valid:", "credit:")
            )
            root_by_recursive_id: dict[str, str] = {}
            for local_id in _controlled_assumption_ids(item, entry.module):
                leaf = formal_ownership.resolve_recursive_assumption_ownership(
                    self.result.ir,
                    item.physical_instance_path,
                    (local_id,),
                    automatic_protocol=automatic,
                    hierarchy=self.hierarchy,
                )
                if (
                    leaf.disposition
                    is not formal_ownership.RecursiveAssumptionDisposition.EXTERNAL
                    or len(leaf.root_leaves) != 1
                ):
                    raise ir_formal.FormalError(
                        "recursive external assumption ownership changed while "
                        f"planning '{item.concrete_property_id}'"
                    )
                observation_id = recursive_observation_id(
                    item.instance_identity,
                    local_id,
                )
                root_by_recursive_id[observation_id] = leaf.root_leaves[0]
                # Aggregate payloads remain backend-published observations;
                # orchestration must not rebuild their physical packing.
                resolved_port = next(
                    (
                        port
                        for port in entry.module.ports
                        if local_id.startswith(f"port:{port.name}.")
                    ),
                    None,
                )
                if (
                    resolved_port is not None
                    and local_id.endswith(".payload")
                    and isinstance(
                        resolved_port.type,
                        (StructType, TupleType, VecType),
                    )
                ):
                    root_by_recursive_id.pop(observation_id)
            predicate = item.property.predicate
            if predicate is not None and root_by_recursive_id:
                predicate = formal_predicates.map_observations(
                    predicate,
                    lambda observation: formal_predicates.ObservationRef(
                        root_by_recursive_id.get(
                            observation.semantic_signal_id,
                            observation.semantic_signal_id,
                        ),
                        observation.width,
                        observation.signedness,
                        observation.cycle,
                    ),
                )
            requirement_id = self._scoped_requirement_id(
                scope_id, item.concrete_property_id
            )
            scoped_external_items.append(
                replace(
                    item,
                    concrete_property_id=requirement_id,
                    property=replace(
                        item.property,
                        id=requirement_id,
                        predicate=predicate,
                        relevant_signals=(
                            ()
                            if predicate is not None
                            else item.property.relevant_signals
                        ),
                    ),
                )
            )
        scoped_external = tuple(scoped_external_items)
        blocker_ids = tuple(
            self._scoped_requirement_id(scope_id, item.concrete_property_id)
            for item, _ in blockers
        )
        local_ids = tuple(
            self._scoped_requirement_id(scope_id, item.semantic_id)
            for item in local_requirements
        )
        assumption_ids = tuple(
            item.concrete_property_id for item in scoped_external
        ) + blocker_ids + local_ids
        node = self.nodes[concrete.instance_identity]
        publication = self.scopes.get(scope_id)
        if publication is None:
            publication = _RecursiveScopePublication(
                scope_id,
                "$recursive:" + ".".join(concrete.physical_instance_path),
                clock_domain,
                reset_domain,
                concrete.physical_instance_path,
                {},
                {},
                node.source_origin,
            )
            self.scopes[scope_id] = publication
        for source, scoped in zip(external, scoped_external, strict=True):
            publication.requirements[scoped.concrete_property_id] = (
                self._requirement_record(
                    scoped.concrete_property_id,
                    source.source_property_id,
                    source.property.source_origin,
                )
            )
        for (source, _), requirement_id in zip(
            blockers, blocker_ids, strict=True
        ):
            publication.requirements[requirement_id] = self._requirement_record(
                requirement_id,
                source.source_property_id,
                source.property.source_origin,
            )
        for source, requirement_id in zip(
            local_requirements, local_ids, strict=True
        ):
            publication.requirements[requirement_id] = self._requirement_record(
                requirement_id,
                source.name,
                source.source_origin,
            )
        blocker_reasons = tuple(reason for _, reason in blockers)
        if local_blocker is not None:
            blocker_reasons = (*blocker_reasons, local_blocker)
        return (
            scope_id,
            scoped_external,
            assumption_ids,
            local_predicate,
            "; ".join(blocker_reasons) if blocker_reasons else None,
            supporting_assertions,
            immediate_requirements,
        )

    def immediate_requirements(self, instance_identity: str) -> tuple[object, ...]:
        return tuple(
            item
            for item, _ in self._discharged_by_instance.get(instance_identity, ())
        )
