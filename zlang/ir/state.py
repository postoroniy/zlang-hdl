"""Backend-independent synchronous state-resource transition IR."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import product

from zlang.ir.expressions import Expression
from zlang.ir.types import BitType, HardwareType
from zlang.source import SourceOrigin


class StateResourceKind(str, Enum):
    REGISTER = "register"
    FIFO = "fifo"
    MEMORY = "memory"
    # A scalar rule-driven output participates in conflict/priority scheduling
    # even though it is not committed state.  Keeping it in the authoritative
    # resource graph prevents a lower rule's otherwise-disjoint state effects
    # from committing after it lost an output conflict.
    OUTPUT = "output"


class StateActionKind(str, Enum):
    REGISTER_WRITE = "register_write"
    FIFO_PUSH = "fifo_push"
    FIFO_POP = "fifo_pop"
    MEMORY_READ_REQUEST = "memory_read_request"
    MEMORY_WRITE = "memory_write"
    OUTPUT_WRITE = "output_write"


class FifoOccupancy(str, Enum):
    """Scheduler-relevant FIFO occupancy classes.

    Rule legality observes only empty, full, and the interior range.  Keeping
    this classification explicit prevents backend boolean logic from growing
    with the numerical FIFO depth while preserving the exact frozen scheduler.
    """

    EMPTY = "empty"
    MIDDLE = "middle"
    FULL = "full"


@dataclass(frozen=True)
class StateResource:
    semantic_id: str
    name: str
    kind: StateResourceKind
    type: HardwareType
    domain: str | None
    source_origin: SourceOrigin | None = None
    depth: int | None = None


@dataclass(frozen=True)
class StateAction:
    semantic_id: str
    resource_id: str
    kind: StateActionKind
    operands: tuple[Expression, ...]
    owner_group: str
    source_origin: SourceOrigin | None = None
    # ``None`` preserves the historical unconditional action representation.
    # A present predicate is evaluated from the same pre-edge snapshot as the
    # owning group's guard and operands.
    activation: Expression | None = None

    def __post_init__(self) -> None:
        if self.activation is not None and self.activation.type != BitType():
            raise ValueError("state-action activation must have type bit")


@dataclass(frozen=True)
class ActionGroup:
    semantic_id: str
    rule_name: str
    guard: Expression
    actions: tuple[StateAction, ...]
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class ResolvedTransition:
    semantic_id: str
    domain: str | None
    reset: str | None
    resources: tuple[StateResource, ...]
    action_groups: tuple[ActionGroup, ...]
    priorities: tuple[tuple[str, str], ...]

    def group(self, rule_name: str) -> ActionGroup:
        return next(item for item in self.action_groups if item.rule_name == rule_name)


def actions_conflict(left: StateAction, right: StateAction) -> bool:
    """Return the frozen single-port/register conflict relation."""
    if left.resource_id != right.resource_id:
        return False
    if left.kind is StateActionKind.REGISTER_WRITE:
        return right.kind is StateActionKind.REGISTER_WRITE
    if {left.kind, right.kind} == {
        StateActionKind.FIFO_PUSH, StateActionKind.FIFO_POP,
    }:
        return False
    return left.kind is right.kind


def conditional_actions(
    transition: ResolvedTransition,
) -> tuple[StateAction, ...]:
    """Return conditional effects in deterministic scheduler-input order."""

    return tuple(
        action
        for group in ordered_groups(transition)
        for action in group.actions
        if action.activation is not None
    )


def conditional_activation_predicates(
    transition: ResolvedTransition,
) -> tuple[Expression, ...]:
    """Return unique activation predicates in deterministic first-use order.

    Several effects in one source branch deliberately carry the same typed
    activation.  They are separate effects, but they are *not* independent
    scheduler inputs.  Keeping one dimension per predicate avoids an
    exponential truth table over impossible combinations such as one effect
    in a branch being active while its sibling is inactive.  Expression
    equality is exact typed-IR structural equality; source origins are already
    excluded from expression dataclass comparison.
    """

    result: list[Expression] = []
    for action in conditional_actions(transition):
        assert action.activation is not None
        if action.activation not in result:
            result.append(action.activation)
    return tuple(result)


def action_activation_predicate_index(
    transition: ResolvedTransition,
    action: StateAction,
) -> int:
    """Return the shared scheduler dimension for one conditional effect."""

    if action.activation is None:
        raise ValueError(
            f"state action '{action.semantic_id}' has no activation predicate"
        )
    try:
        return conditional_activation_predicates(transition).index(
            action.activation
        )
    except ValueError as error:  # pragma: no cover - defensive malformed IR
        raise ValueError(
            f"state action '{action.semantic_id}' activation is not part of "
            "its transition"
        ) from error


def _validated_activation_values(
    transition: ResolvedTransition,
    activation_values: dict[str, bool] | None,
) -> dict[str, bool]:
    conditional = conditional_actions(transition)
    expected = {action.semantic_id for action in conditional}
    provided = set(activation_values or ())
    unknown = provided - expected
    if unknown:
        raise ValueError(
            "state transition received unknown action activation "
            f"'{sorted(unknown)[0]}'"
        )
    missing = expected - provided
    if missing:
        raise ValueError(
            "state transition requires action activation "
            f"'{sorted(missing)[0]}'"
        )
    return {
        action.semantic_id: bool((activation_values or {})[action.semantic_id])
        for action in conditional
    }


def _active_actions(
    group: ActionGroup,
    activation_values: dict[str, bool],
) -> tuple[StateAction, ...]:
    return tuple(
        action
        for action in group.actions
        if action.activation is None or activation_values[action.semantic_id]
    )


def groups_conflict(
    left: ActionGroup,
    right: ActionGroup,
    activation_values: dict[str, bool] | None = None,
) -> bool:
    """Return a potential, or for supplied predicates an active, conflict.

    The two-argument compatibility form deliberately retains the historical
    conservative relation used by semantic validation and existing M35 code.
    Runtime scheduling passes exact activation values and ignores inactive
    effects.
    """

    if activation_values is None:
        left_actions, right_actions = left.actions, right.actions
    else:
        left_actions = _active_actions(left, activation_values)
        right_actions = _active_actions(right, activation_values)
    return any(
        actions_conflict(a, b) for a in left_actions for b in right_actions
    )


def ordered_groups(transition: ResolvedTransition) -> tuple[ActionGroup, ...]:
    remaining = {item.rule_name: item for item in transition.action_groups}
    ordered: list[ActionGroup] = []
    while remaining:
        ready = sorted(
            name for name in remaining
            if not any(
                lower == name and higher in remaining
                for higher, lower in transition.priorities
            )
        )
        if not ready:
            raise ValueError("state transition priority graph contains a cycle")
        for name in ready:
            ordered.append(remaining.pop(name))
    return tuple(ordered)


def select_action_groups(
    transition: ResolvedTransition,
    guard_values: dict[str, bool],
    fifo_counts: dict[str, int],
    activation_values: dict[str, bool] | None = None,
) -> tuple[str, ...]:
    """Return the priority-maximal legal atomic action-group set."""
    active = _validated_activation_values(transition, activation_values)
    candidates = tuple(
        item for item in ordered_groups(transition)
        if (
            guard_values.get(item.rule_name, False)
            and _active_actions(item, active)
        )
    )
    if len(candidates) > 20:
        raise ValueError("state transition supports at most 20 enabled rules")
    resources = {item.semantic_id: item for item in transition.resources}

    def legal(selected: tuple[ActionGroup, ...]) -> bool:
        for index, left in enumerate(selected):
            if any(
                groups_conflict(left, right, active)
                for right in selected[index + 1:]
            ):
                return False
        fifo_actions: dict[str, set[StateActionKind]] = {}
        for group in selected:
            for action in _active_actions(group, active):
                resource = resources[action.resource_id]
                if resource.kind is StateResourceKind.FIFO:
                    fifo_actions.setdefault(resource.name, set()).add(action.kind)
        for resource in transition.resources:
            if resource.kind is not StateResourceKind.FIFO:
                continue
            actions = fifo_actions.get(resource.name, set())
            count = fifo_counts.get(resource.name, 0)
            pop = StateActionKind.FIFO_POP in actions
            push = StateActionKind.FIFO_PUSH in actions
            if pop and count == 0:
                return False
            if push and resource.depth is not None and count >= resource.depth and not pop:
                return False
        return True

    best: tuple[ActionGroup, ...] = ()
    best_score: tuple[int, ...] | None = None
    for mask in range(1 << len(candidates)):
        selected = tuple(
            item for index, item in enumerate(candidates) if mask & (1 << index)
        )
        if not legal(selected):
            continue
        score = tuple(int(item in selected) for item in candidates)
        if best_score is None or score > best_score:
            best, best_score = selected, score
    return tuple(item.rule_name for item in best)


def selection_cubes(
    transition: ResolvedTransition, rule_name: str
) -> tuple[tuple[int | bool | None, ...], ...]:
    """Minimize the exact finite scheduler truth table into deterministic cubes."""
    fifos = tuple(
        item for item in transition.resources
        if item.kind is StateResourceKind.FIFO
    )
    groups = ordered_groups(transition)
    conditional = conditional_actions(transition)
    activation_predicates = conditional_activation_predicates(transition)
    activation_indices = {
        action.semantic_id: activation_predicates.index(action.activation)
        for action in conditional
    }
    domains: tuple[tuple[int | bool, ...], ...] = (
        *(tuple(range((item.depth or 0) + 1)) for item in fifos),
        *((False, True) for _ in groups),
        *((False, True) for _ in activation_predicates),
    )
    cubes: set[tuple[int | bool | None, ...]] = set()
    for values in product(*domains):
        counts = {
            fifo.name: int(values[index]) for index, fifo in enumerate(fifos)
        }
        guards = {
            group.rule_name: bool(values[len(fifos) + index])
            for index, group in enumerate(groups)
        }
        activation_offset = len(fifos) + len(groups)
        activations = {
            action.semantic_id: bool(values[
                activation_offset
                + activation_indices[action.semantic_id]
            ])
            for action in conditional
        }
        if rule_name in select_action_groups(
            transition, guards, counts, activations
        ):
            cubes.add(tuple(values))
    changed = True
    while changed:
        changed = False
        for dimension, domain in enumerate(domains):
            buckets: dict[tuple[object, ...], list[tuple[int | bool | None, ...]]] = {}
            for cube in cubes:
                if cube[dimension] is None:
                    continue
                key = cube[:dimension] + cube[dimension + 1:]
                buckets.setdefault(key, []).append(cube)
            replacements: list[tuple[set[tuple[int | bool | None, ...]], tuple[int | bool | None, ...]]] = []
            for key, members in buckets.items():
                if {item[dimension] for item in members} == set(domain):
                    replacement = key[:dimension] + (None,) + key[dimension:]
                    replacements.append((set(members), replacement))
            if replacements:
                for removed, replacement in replacements:
                    cubes.difference_update(removed)
                    cubes.add(replacement)
                changed = True
    # Remove cubes subsumed by a more general cube.
    result = []
    for cube in cubes:
        if any(
            other != cube and all(a is None or a == b for a, b in zip(other, cube, strict=True))
            for other in cubes
        ):
            continue
        result.append(cube)
    return tuple(sorted(result, key=lambda cube: tuple(-1 if item is None else int(item) for item in cube)))


def selection_regions(
    transition: ResolvedTransition, rule_name: str
) -> tuple[tuple[FifoOccupancy | bool | None, ...], ...]:
    """Return exact scheduler regions without enumerating every FIFO count.

    FIFO legality depends only on ``count == 0`` and ``count == depth``.  The
    old truth-table helper remains available for compatibility and semantic
    tests; emitters use this depth-independent form so a depth-32 FIFO does not
    generate tens of thousands of repeated count comparisons.
    """

    fifos = tuple(
        item for item in transition.resources
        if item.kind is StateResourceKind.FIFO
    )
    groups = ordered_groups(transition)
    fifo_domains: tuple[tuple[FifoOccupancy, ...], ...] = tuple(
        (
            (FifoOccupancy.EMPTY, FifoOccupancy.FULL)
            if item.depth == 1
            else (
                FifoOccupancy.EMPTY,
                FifoOccupancy.MIDDLE,
                FifoOccupancy.FULL,
            )
        )
        for item in fifos
    )
    conditional = conditional_actions(transition)
    activation_predicates = conditional_activation_predicates(transition)
    activation_indices = {
        action.semantic_id: activation_predicates.index(action.activation)
        for action in conditional
    }
    domains: tuple[tuple[FifoOccupancy | bool, ...], ...] = (
        *fifo_domains,
        *((False, True) for _ in groups),
        *((False, True) for _ in activation_predicates),
    )
    regions: set[tuple[FifoOccupancy | bool | None, ...]] = set()
    for values in product(*domains):
        counts = {
            fifo.name: (
                0
                if values[index] is FifoOccupancy.EMPTY
                else (
                    fifo.depth or 0
                    if values[index] is FifoOccupancy.FULL
                    else 1
                )
            )
            for index, fifo in enumerate(fifos)
        }
        guards = {
            group.rule_name: bool(values[len(fifos) + index])
            for index, group in enumerate(groups)
        }
        activation_offset = len(fifos) + len(groups)
        activations = {
            action.semantic_id: bool(values[
                activation_offset
                + activation_indices[action.semantic_id]
            ])
            for action in conditional
        }
        if rule_name in select_action_groups(
            transition, guards, counts, activations
        ):
            regions.add(tuple(values))

    changed = True
    while changed:
        changed = False
        for dimension, domain in enumerate(domains):
            buckets: dict[
                tuple[object, ...],
                list[tuple[FifoOccupancy | bool | None, ...]],
            ] = {}
            for region in regions:
                if region[dimension] is None:
                    continue
                key = region[:dimension] + region[dimension + 1:]
                buckets.setdefault(key, []).append(region)
            replacements: list[
                tuple[
                    set[tuple[FifoOccupancy | bool | None, ...]],
                    tuple[FifoOccupancy | bool | None, ...],
                ]
            ] = []
            for key, members in buckets.items():
                if {item[dimension] for item in members} == set(domain):
                    replacement = key[:dimension] + (None,) + key[dimension:]
                    replacements.append((set(members), replacement))
            if replacements:
                for removed, replacement in replacements:
                    regions.difference_update(removed)
                    regions.add(replacement)
                changed = True

    result = []
    for region in regions:
        if any(
            other != region
            and all(a is None or a == b for a, b in zip(other, region, strict=True))
            for other in regions
        ):
            continue
        result.append(region)

    order = {
        None: -1,
        False: 0,
        True: 1,
        FifoOccupancy.EMPTY: 2,
        FifoOccupancy.MIDDLE: 3,
        FifoOccupancy.FULL: 4,
    }
    return tuple(sorted(result, key=lambda item: tuple(order[value] for value in item)))
