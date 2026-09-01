"""Backend-independent synchronous state-resource transition IR."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import product

from zlang.ir.expressions import Expression
from zlang.ir.types import HardwareType
from zlang.source import SourceOrigin


class StateResourceKind(str, Enum):
    REGISTER = "register"
    FIFO = "fifo"
    MEMORY = "memory"


class StateActionKind(str, Enum):
    REGISTER_WRITE = "register_write"
    FIFO_PUSH = "fifo_push"
    FIFO_POP = "fifo_pop"
    MEMORY_READ_REQUEST = "memory_read_request"
    MEMORY_WRITE = "memory_write"


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


def groups_conflict(left: ActionGroup, right: ActionGroup) -> bool:
    return any(actions_conflict(a, b) for a in left.actions for b in right.actions)


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
) -> tuple[str, ...]:
    """Return the priority-maximal legal atomic action-group set."""
    candidates = tuple(
        item for item in ordered_groups(transition)
        if guard_values.get(item.rule_name, False)
    )
    if len(candidates) > 20:
        raise ValueError("state transition supports at most 20 enabled rules")
    resources = {item.semantic_id: item for item in transition.resources}

    def legal(selected: tuple[ActionGroup, ...]) -> bool:
        for index, left in enumerate(selected):
            if any(groups_conflict(left, right) for right in selected[index + 1:]):
                return False
        fifo_actions: dict[str, set[StateActionKind]] = {}
        for group in selected:
            for action in group.actions:
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
    domains: tuple[tuple[int | bool, ...], ...] = (
        *(tuple(range((item.depth or 0) + 1)) for item in fifos),
        *((False, True) for _ in groups),
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
        if rule_name in select_action_groups(transition, guards, counts):
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
    domains: tuple[tuple[FifoOccupancy | bool, ...], ...] = (
        *fifo_domains,
        *((False, True) for _ in groups),
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
        if rule_name in select_action_groups(transition, guards, counts):
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
