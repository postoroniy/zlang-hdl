# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Uncoupled effects must not build a global product of selection choices."""

from dataclasses import replace
from itertools import product

import pytest

import zlang.ir.state as state
from tests.support.independent_state_cases import source
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.ir import BitType, Constant, InputRef


def transition(count=3):
    bit = BitType()
    resources = tuple(state.StateResource(
        f"r{i}", f"r{i}", state.StateResourceKind.REGISTER, bit, "clk",
    ) for i in range(count * 2))
    groups = []
    for i in range(count):
        actions = tuple(state.StateAction(
            f"action:{i}:{j}", resources[i * 2 + j].semantic_id,
            state.StateActionKind.REGISTER_WRITE, (Constant(1, bit),), f"group:{i}",
            activation=InputRef(f"p{(i + j) % (count + 1)}", bit),
        ) for j in range(2))
        groups.append(state.ActionGroup(
            f"group:{i}", f"group{i:02}", InputRef(f"g{i}", bit), actions, domain="clk",
        ))
    return state.ResolvedTransition("independent", "clk", "rst", resources, tuple(groups), ())


def covered(regions, values):
    return any(all(a is None or a == b for a, b in zip(region, values, strict=True))
               for region in regions)


@pytest.mark.parametrize("special", ("ordinary", "empty", "unconditional", "priority", "conflict", "fifo"))
def test_regions_match_an_independent_exhaustive_selection(special):
    t = transition()
    if special == "empty":
        t = replace(t, action_groups=(replace(t.action_groups[0], actions=()), *t.action_groups[1:]))
    elif special == "unconditional":
        group = t.action_groups[0]
        t = replace(t, action_groups=(replace(group, actions=(replace(group.actions[0], activation=None),)), *t.action_groups[1:]))
    elif special == "priority":
        t = replace(t, priorities=(("group02", "group00"),))
    elif special == "conflict":
        group = t.action_groups[1]
        t = replace(t, action_groups=(t.action_groups[0], replace(group, actions=(replace(group.actions[0], resource_id="r0"),)), t.action_groups[2]), priorities=(("group00", "group01"),))
    elif special == "fifo":
        q = state.StateResource("q", "q", state.StateResourceKind.FIFO, BitType(), "clk", depth=1)
        first, second, third = t.action_groups
        t = replace(t, resources=(*t.resources, q), action_groups=(
            replace(first, actions=(replace(first.actions[0], resource_id="q", kind=state.StateActionKind.FIFO_PUSH),)),
            replace(second, actions=(replace(second.actions[0], resource_id="q", kind=state.StateActionKind.FIFO_POP, operands=()),)), third,
        ))
    groups = state.ordered_groups(t)
    predicates = state.conditional_activation_predicates(t)
    regions = state.selection_regions_for_transition(t)
    for values in product((False, True), repeat=len(groups) + len(predicates)):
        guards, active = values[:len(groups)], values[len(groups):]
        candidates = [tuple(a for a in g.actions if a.activation is None or active[predicates.index(a.activation)])
                      if guards[i] else () for i, g in enumerate(groups)]
        for occupancy in ((False, True) if special == "fifo" else (False,)):
            legal = []
            for chosen in product((False, True), repeat=len(groups)):
                if any(chosen[i] and not candidates[i] for i in range(len(groups))):
                    continue
                actions = [(i, a) for i, row in enumerate(candidates) if chosen[i] for a in row]
                if any(i != j and a.resource_id == b.resource_id and a.kind == b.kind
                       for i, a in actions for j, b in actions):
                    continue
                pushes = any(a.kind is state.StateActionKind.FIFO_PUSH for _, a in actions)
                pops = any(a.kind is state.StateActionKind.FIFO_POP for _, a in actions)
                if special == "fifo" and ((pops and not occupancy) or (pushes and occupancy and not pops)):
                    continue
                legal.append(chosen)
            expected = {g.rule_name for g, yes in zip(groups, max(legal), strict=True) if yes}
            dimensions = ((state.FifoOccupancy.FULL if occupancy else state.FifoOccupancy.EMPTY,) if special == "fifo" else ()) + values
            assert {name for name, rows in regions.items() if covered(rows, dimensions)} == expected


def test_disjoint_conditional_owners_do_not_project_selection_variables(monkeypatch):
    compiled = compile_source(source())
    nodes = []
    original_node = state._SchedulerBdd.node
    def track(self, *args):
        value = original_node(self, *args)
        nodes.append(len(self.nodes))
        return value
    def forbidden(*args):
        raise AssertionError("disjoint owners must not quantify a global selection product")
    monkeypatch.setattr(state._SchedulerBdd, "node", track)
    monkeypatch.setattr(state._SchedulerBdd, "exists_choices", forbidden)
    artifact = emit_artifact(compiled.ir)
    assert artifact.module == "IndependentOwners"
    assert max(nodes) < 1000


def test_symbolic_node_cap_is_still_exactly_one_hundred_thousand():
    bdd = state._SchedulerBdd(100_001, None)
    for axis in range(99_998):
        bdd.node(axis, 0, 1)
    assert len(bdd.nodes) == 100_000
    with pytest.raises(state.StateSelectionLimitError, match="100000 symbolic nodes"):
        bdd.node(99_998, 0, 1)


def test_independent_fast_path_preserves_the_decision_variable_limit():
    with pytest.raises(state.StateSelectionLimitError, match="256 decision variables"):
        state.selection_regions_for_transition(transition(86))
