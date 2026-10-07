"""Backend-independent synchronous state-resource transition IR."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from itertools import product

from zlang.diagnostics import DiagnosticError
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


class StateSelectionLimitError(DiagnosticError):
    """Exact scheduling exceeded its bounded compiler work budget."""

    default_code = "ZL-STATE-SCHEDULER-LIMIT"


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
    domain: str | None = None


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


def transition_for_domain(
    transition: ResolvedTransition,
    domain: str,
) -> ResolvedTransition:
    """Project one independent clock-domain scheduler from a module transition.

    The semantic module keeps one authoritative resource/action ledger so IDs
    remain globally unique.  Scheduling, simulation and RTL emission operate on
    this lossless per-domain view; no cross-clock priority or atomicity is
    invented.
    """

    groups = tuple(
        group for group in transition.action_groups if group.domain == domain
    )
    resource_ids = {
        action.resource_id for group in groups for action in group.actions
    }
    resources = tuple(
        resource
        for resource in transition.resources
        if resource.domain == domain or resource.semantic_id in resource_ids
    )
    priorities = tuple(
        (higher, lower)
        for higher, lower in transition.priorities
        if any(group.rule_name == higher for group in groups)
        and any(group.rule_name == lower for group in groups)
    )
    return ResolvedTransition(
        f"{transition.semantic_id}:domain:{domain}",
        domain,
        None,
        resources,
        groups,
        priorities,
    )


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
    conservative relation used by semantic validation and existing safety verification code.
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
    # Without FIFOs, legality is solely pairwise conflict exclusion.  It is
    # downward-closed, so selecting each highest-priority compatible group in
    # order gives exactly the lexicographically maximal legal set.  Enumerating
    # every subset here makes even a modest FSM exponential during backend
    # scheduler-region construction.
    if not any(
        resource.kind is StateResourceKind.FIFO
        for resource in transition.resources
    ):
        selected: list[ActionGroup] = []
        for candidate in candidates:
            if not any(
                groups_conflict(candidate, prior, active)
                for prior in selected
            ):
                selected.append(candidate)
        return tuple(item.rule_name for item in selected)

    resources = {item.semantic_id: item for item in transition.resources}
    fifos = tuple(
        item for item in transition.resources
        if item.kind is StateResourceKind.FIFO
    )
    fifo_indices = {item.semantic_id: index for index, item in enumerate(fifos)}
    empty = sum(
        1 << index for index, item in enumerate(fifos)
        if fifo_counts.get(item.name, 0) == 0
    )
    full = sum(
        1 << index for index, item in enumerate(fifos)
        if item.depth is not None and fifo_counts.get(item.name, 0) >= item.depth
    )
    conflicts = tuple(
        sum(
            1 << later for later in range(index + 1, len(candidates))
            if groups_conflict(group, candidates[later], active)
        )
        for index, group in enumerate(candidates)
    )
    effects: list[tuple[int, int]] = []
    for group in candidates:
        pushes = pops = 0
        for action in _active_actions(group, active):
            if resources[action.resource_id].kind is not StateResourceKind.FIFO:
                continue
            bit = 1 << fifo_indices[action.resource_id]
            if action.kind is StateActionKind.FIFO_PUSH:
                pushes |= bit
            elif action.kind is StateActionKind.FIFO_POP:
                pops |= bit
        effects.append((pushes, pops))

    # Legality is not downward-closed: a lower-priority pop may make a
    # higher-priority push legal on a full FIFO.  Try the lexicographically
    # preferred inclusion first, but accept it only if a legal suffix exists.
    work = 0

    @lru_cache(maxsize=None)
    def suffix(
        index: int, blocked: int, pushes: int, pops: int,
    ) -> tuple[int, ...] | None:
        nonlocal work
        work += 1
        if work > 100_000:
            raise StateSelectionLimitError(
                "state scheduler exceeds 100000 exact selection states",
                primary=candidates[0].source_origin if candidates else None,
            )
        if index == len(candidates):
            return () if not (pushes & full & ~pops) else None
        if not (blocked & (1 << index)):
            next_pushes, next_pops = effects[index]
            if not (next_pops & empty):
                chosen = suffix(
                    index + 1,
                    blocked | conflicts[index],
                    pushes | next_pushes,
                    pops | next_pops,
                )
                if chosen is not None:
                    return (index, *chosen)
        return suffix(index + 1, blocked, pushes, pops)

    chosen = suffix(0, 0, 0, 0)
    assert chosen is not None  # The empty set is always legal.
    return tuple(candidates[index].rule_name for index in chosen)


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


class _SchedulerBdd:
    """Bounded, per-transition Boolean decisions; no process-wide cache."""

    def __init__(
        self, terminal_axis: int, origin: SourceOrigin | None,
        selection_axes: tuple[int, ...] = (),
    ) -> None:
        self.origin = origin
        self.selection_axis_index = {
            axis: index for index, axis in enumerate(selection_axes)
        }
        self.nodes = [(terminal_axis, 0, 0), (terminal_axis, 1, 1)]
        self.unique: dict[tuple[int, int, int], int] = {}
        self.apply_cache: dict[tuple[str, int, int], int] = {}
        self.not_cache = {0: 1, 1: 0}
        self.cofactor_cache: dict[tuple[int, int, bool], int] = {}
        self.exists_cache: dict[tuple[int, int], int] = {}

    def node(self, axis: int, low: int, high: int) -> int:
        if low == high:
            return low
        key = (axis, low, high)
        if key not in self.unique:
            if len(self.nodes) >= 100_000:
                raise StateSelectionLimitError(
                    "state scheduler exceeds 100000 symbolic nodes",
                    primary=self.origin,
                )
            self.unique[key] = len(self.nodes)
            self.nodes.append(key)
        return self.unique[key]

    def variable(self, axis: int) -> int:
        return self.node(axis, 0, 1)

    def negate(self, value: int) -> int:
        if value not in self.not_cache:
            axis, low, high = self.nodes[value]
            self.not_cache[value] = self.node(
                axis, self.negate(low), self.negate(high)
            )
        return self.not_cache[value]

    def apply(self, operation: str, left: int, right: int) -> int:
        if operation == "and":
            if left == 0 or right == 0:
                return 0
            if left == 1:
                return right
            if right == 1:
                return left
        else:
            if left == 1 or right == 1:
                return 1
            if left == 0:
                return right
            if right == 0:
                return left
        if left == right:
            return left
        key = (operation, min(left, right), max(left, right))
        if key not in self.apply_cache:
            axis = min(self.nodes[left][0], self.nodes[right][0])
            left_low, left_high = (
                self.nodes[left][1:] if self.nodes[left][0] == axis
                else (left, left)
            )
            right_low, right_high = (
                self.nodes[right][1:] if self.nodes[right][0] == axis
                else (right, right)
            )
            self.apply_cache[key] = self.node(
                axis,
                self.apply(operation, left_low, right_low),
                self.apply(operation, left_high, right_high),
            )
            if len(self.apply_cache) > 500_000:
                raise StateSelectionLimitError(
                    "state scheduler exceeds 500000 symbolic operations",
                    primary=self.origin,
                )
        return self.apply_cache[key]

    def cofactor(self, value: int, axis: int, choice: bool) -> int:
        key = (value, axis, choice)
        if key not in self.cofactor_cache:
            current, low, high = self.nodes[value]
            if current >= axis:
                result = (high if choice else low) if current == axis else value
            else:
                result = self.node(
                    current,
                    self.cofactor(low, axis, choice),
                    self.cofactor(high, axis, choice),
                )
            self.cofactor_cache[key] = result
            if len(self.cofactor_cache) > 500_000:
                raise StateSelectionLimitError(
                    "state scheduler exceeds 500000 symbolic cofactors",
                    primary=self.origin,
                )
        return self.cofactor_cache[key]

    def exists_choices(self, value: int, first_index: int) -> int:
        key = (value, first_index)
        if key not in self.exists_cache:
            current, low, high = self.nodes[value]
            if value < 2:
                result = value
            elif self.selection_axis_index.get(current, -1) >= first_index:
                result = self.apply(
                    "or", self.exists_choices(low, first_index),
                    self.exists_choices(high, first_index),
                )
            else:
                result = self.node(
                    current, self.exists_choices(low, first_index),
                    self.exists_choices(high, first_index),
                )
            self.exists_cache[key] = result
            if len(self.exists_cache) > 500_000:
                raise StateSelectionLimitError(
                    "state scheduler exceeds 500000 symbolic projections",
                    primary=self.origin,
                )
        return self.exists_cache[key]


def selection_regions_for_transition(
    transition: ResolvedTransition,
) -> dict[str, tuple[tuple[FifoOccupancy | bool | None, ...], ...]]:
    """Compute all exact priority-maximal fire regions once per transition."""

    groups = ordered_groups(transition)
    fifos = tuple(
        item for item in transition.resources
        if item.kind is StateResourceKind.FIFO
    )
    predicates = conditional_activation_predicates(transition)
    if len(groups) > 256:
        raise StateSelectionLimitError(
            "state scheduler exceeds 256 action groups",
            primary=groups[0].source_origin,
        )
    if not fifos and not predicates:
        guard_regions = {}
        total_regions = 0
        for group in groups:
            regions = _guard_only_selection_regions(groups, group.rule_name)
            total_regions += len(regions)
            if total_regions > 20_000:
                raise StateSelectionLimitError(
                    "state scheduler exceeds 20000 exact regions in one transition",
                    primary=group.source_origin,
                )
            guard_regions[group.rule_name] = regions
        return guard_regions

    fifo_axes: list[tuple[int, ...]] = []
    axis = 0
    for fifo in fifos:
        width = 1 if fifo.depth == 1 else 2
        fifo_axes.append(tuple(range(axis, axis + width)))
        axis += width
    activation_axes = tuple(range(axis, axis + len(predicates)))
    group_start = axis + len(predicates)
    guard_axes = tuple(group_start + 2 * index for index in range(len(groups)))
    selection_axes = tuple(axis + 1 for axis in guard_axes)
    terminal_axis = group_start + 2 * len(groups)
    if terminal_axis > 256:
        raise StateSelectionLimitError(
            "state scheduler exceeds 256 decision variables",
            primary=groups[0].source_origin if groups else None,
        )
    bdd = _SchedulerBdd(
        terminal_axis,
        groups[0].source_origin if groups else None,
        selection_axes,
    )

    def land(left: int, right: int) -> int:
        return bdd.apply("and", left, right)

    def lor(left: int, right: int) -> int:
        return bdd.apply("or", left, right)

    fifo_index = {fifo.semantic_id: index for index, fifo in enumerate(fifos)}
    action_active = {
        action.semantic_id: (
            1 if action.activation is None else
            bdd.variable(activation_axes[predicates.index(action.activation)])
        )
        for group in groups for action in group.actions
    }
    selected = [bdd.variable(axis) for axis in selection_axes]
    formula = 1
    empty: list[int] = []
    full: list[int] = []
    for axes in fifo_axes:
        if len(axes) == 1:
            full.append(bdd.variable(axes[0]))
            empty.append(bdd.negate(full[-1]))
        else:
            empty.append(bdd.variable(axes[0]))
            full.append(bdd.variable(axes[1]))
            formula = land(formula, bdd.negate(land(empty[-1], full[-1])))

    pushes = [0] * len(fifos)
    pops = [0] * len(fifos)
    for index, group in enumerate(groups):
        active = 0
        for action in group.actions:
            condition = action_active[action.semantic_id]
            active = lor(active, condition)
            if action.resource_id in fifo_index:
                fifo = fifo_index[action.resource_id]
                effect = land(selected[index], condition)
                if action.kind is StateActionKind.FIFO_PUSH:
                    pushes[fifo] = lor(pushes[fifo], effect)
                elif action.kind is StateActionKind.FIFO_POP:
                    pops[fifo] = lor(pops[fifo], effect)
        enabled = land(bdd.variable(guard_axes[index]), active)
        formula = land(formula, lor(bdd.negate(selected[index]), enabled))
        for earlier in range(index):
            conflict = 0
            for left in groups[earlier].actions:
                for right in group.actions:
                    if actions_conflict(left, right):
                        conflict = lor(
                            conflict,
                            land(
                                action_active[left.semantic_id],
                                action_active[right.semantic_id],
                            ),
                        )
            if conflict:
                formula = land(
                    formula,
                    bdd.negate(land(land(selected[earlier], selected[index]), conflict)),
                )
    for index in range(len(fifos)):
        formula = land(formula, lor(bdd.negate(pops[index]), bdd.negate(empty[index])))
        formula = land(
            formula,
            lor(bdd.negate(pushes[index]), lor(bdd.negate(full[index]), pops[index])),
        )

    fire_functions: list[int] = []
    for index in range(len(groups)):
        choice_axis = selection_axes[index]
        yes = bdd.cofactor(formula, choice_axis, True)
        no = bdd.cofactor(formula, choice_axis, False)
        can_choose = bdd.exists_choices(yes, index + 1)
        fire_functions.append(can_choose)
        formula = lor(land(can_choose, yes), land(bdd.negate(can_choose), no))

    order = {
        None: -1, False: 0, True: 1,
        FifoOccupancy.EMPTY: 2,
        FifoOccupancy.MIDDLE: 3,
        FifoOccupancy.FULL: 4,
    }
    result = {}
    total_regions = 0
    for group, root in zip(groups, fire_functions, strict=True):
        regions: set[tuple[FifoOccupancy | bool | None, ...]] = set()
        assignment: dict[int, bool] = {}

        def collect(value: int) -> None:
            nonlocal total_regions
            if value == 0:
                return
            if value == 1:
                choices: list[tuple[FifoOccupancy | None, ...]] = []
                for fifo, axes in zip(fifos, fifo_axes, strict=True):
                    states = (
                        ((FifoOccupancy.EMPTY, (False,)),
                         (FifoOccupancy.FULL, (True,)))
                        if fifo.depth == 1 else
                        ((FifoOccupancy.EMPTY, (True, False)),
                         (FifoOccupancy.MIDDLE, (False, False)),
                         (FifoOccupancy.FULL, (False, True)))
                    )
                    matching = tuple(
                        state for state, bits in states
                        if all(
                            axis not in assignment or assignment[axis] == bit
                            for axis, bit in zip(axes, bits, strict=True)
                        )
                    )
                    if not matching:
                        return
                    choices.append((None,) if len(matching) == len(states) else matching)
                tail = tuple(
                    assignment.get(axis)
                    for axis in (*guard_axes, *activation_axes)
                )
                for occupancy in product(*choices):
                    region = (*occupancy, *tail)
                    if region not in regions:
                        regions.add(region)
                        total_regions += 1
                    if total_regions > 20_000:
                        raise StateSelectionLimitError(
                            "state scheduler exceeds 20000 exact regions in one transition",
                            primary=group.source_origin,
                        )
                return
            axis, low, high = bdd.nodes[value]
            assignment[axis] = False
            collect(low)
            assignment[axis] = True
            collect(high)
            del assignment[axis]

        collect(root)
        result[group.rule_name] = tuple(
            sorted(regions, key=lambda region: tuple(order[item] for item in region))
        )
    return result


def selection_regions(
    transition: ResolvedTransition, rule_name: str,
) -> tuple[tuple[FifoOccupancy | bool | None, ...], ...]:
    """Return exact occupancy/guard/activation regions for one rule."""
    return selection_regions_for_transition(transition).get(rule_name, ())


def _guard_only_selection_regions(
    groups: tuple[ActionGroup, ...], rule_name: str
) -> tuple[tuple[bool | None, ...], ...]:
    """Build exact priority selections without a 2**rules truth table.

    With no FIFO occupancy or conditional effects, only guards and pairwise
    conflicts influence selection.  Each group's fire function is its guard
    conjoined with the absence of any already-selected conflicting group.
    A reduced ordered Boolean decision diagram shares those functions and
    yields deterministic disjoint regions directly.
    """

    if not any(group.rule_name == rule_name for group in groups):
        return ()

    bdd = _SchedulerBdd(len(groups), groups[0].source_origin)

    fires: list[int] = []
    for index, group in enumerate(groups):
        blockers = 0
        for prior, prior_fire in zip(groups[:index], fires, strict=True):
            if groups_conflict(group, prior):
                blockers = bdd.apply("or", blockers, prior_fire)
        fires.append(
            bdd.apply(
                "and", bdd.variable(index), bdd.negate(blockers)
            )
            if group.actions else 0
        )

    target = next(
        fire for group, fire in zip(groups, fires, strict=True)
        if group.rule_name == rule_name
    )
    values: list[bool | None] = [None] * len(groups)
    regions: list[tuple[bool | None, ...]] = []

    def collect(value: int) -> None:
        if value == 0:
            return
        if value == 1:
            regions.append(tuple(values))
            if len(regions) > 20_000:
                raise StateSelectionLimitError(
                    "state scheduler exceeds 20000 exact regions",
                    primary=groups[0].source_origin,
                )
            return
        axis, low, high = bdd.nodes[value]
        values[axis] = False
        collect(low)
        values[axis] = True
        collect(high)
        values[axis] = None

    collect(target)
    return tuple(
        sorted(
            regions,
            key=lambda region: tuple(-1 if value is None else int(value) for value in region),
        )
    )
