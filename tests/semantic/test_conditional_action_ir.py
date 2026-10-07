from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from zlang.ir.expressions import Constant, Expression, InputRef
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import NextAssignment, Port, PortDirection
from zlang.ir.state import (
    ActionGroup,
    ResolvedTransition,
    StateAction,
    StateActionKind,
    StateResource,
    StateResourceKind,
    conditional_activation_predicates,
    conditional_actions,
    groups_conflict,
    select_action_groups,
    selection_regions,
)
from zlang.ir.types import BitType, UIntType
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.parser import parse
from zlang.semantic import analyze


def _action(
    name: str,
    resource: StateResource,
    kind: StateActionKind,
    operands: tuple[Expression, ...],
    owner: str,
    activation: Expression | None = None,
) -> StateAction:
    return StateAction(
        name,
        resource.semantic_id,
        kind,
        operands,
        owner,
        activation=activation,
    )


def test_conditional_effects_control_fifo_legality_without_else_fallback() -> None:
    bit = BitType()
    byte = UIntType(8)
    fifo = StateResource("fifo:q", "q", StateResourceKind.FIFO, byte, "clk", depth=1)
    register = StateResource(
        "register:r", "r", StateResourceKind.REGISTER, bit, "clk"
    )
    take = InputRef("take", bit)
    high = ActionGroup(
        "group:high",
        "high",
        Constant(1, bit),
        (_action(
            "action:pop", fifo, StateActionKind.FIFO_POP, (), "group:high", take
        ),),
    )
    low = ActionGroup(
        "group:low",
        "low",
        Constant(1, bit),
        (_action(
            "action:write",
            register,
            StateActionKind.REGISTER_WRITE,
            (Constant(1, bit),),
            "group:low",
        ),),
    )
    transition = ResolvedTransition(
        "transition:test",
        "clk",
        "rst",
        (fifo, register),
        (high, low),
        (("high", "low"),),
    )

    assert conditional_actions(transition) == (high.actions[0],)
    # No selected nested branch means the outer logical rule has no effect and
    # therefore does not fire.
    assert select_action_groups(
        transition,
        {"high": True, "low": True},
        {"q": 0},
        {"action:pop": False},
    ) == ("low",)
    # The selected pop is illegal on empty.  The scheduler may accept the
    # independent lower rule, but it must not reinterpret that as an inner
    # ``else`` branch of the high rule.
    assert select_action_groups(
        transition,
        {"high": True, "low": True},
        {"q": 0},
        {"action:pop": True},
    ) == ("low",)
    assert select_action_groups(
        transition,
        {"high": True, "low": True},
        {"q": 1},
        {"action:pop": True},
    ) == ("high", "low")


def test_output_conflict_participates_only_while_its_effect_is_active() -> None:
    bit = BitType()
    output = StateResource(
        "output:event", "event", StateResourceKind.OUTPUT, bit, "clk"
    )
    choose_high = InputRef("choose_high", bit)
    high = ActionGroup(
        "group:high",
        "high",
        Constant(1, bit),
        (_action(
            "action:high-output",
            output,
            StateActionKind.OUTPUT_WRITE,
            (Constant(1, bit),),
            "group:high",
            choose_high,
        ),),
    )
    low = ActionGroup(
        "group:low",
        "low",
        Constant(1, bit),
        (_action(
            "action:low-output",
            output,
            StateActionKind.OUTPUT_WRITE,
            (Constant(0, bit),),
            "group:low",
        ),),
    )
    transition = ResolvedTransition(
        "transition:output",
        "clk",
        "rst",
        (output,),
        (high, low),
        (("high", "low"),),
    )

    assert groups_conflict(high, low)
    assert not groups_conflict(
        high, low, {"action:high-output": False}
    )
    assert groups_conflict(high, low, {"action:high-output": True})
    assert select_action_groups(
        transition,
        {"high": True, "low": True},
        {},
        {"action:high-output": False},
    ) == ("low",)
    assert select_action_groups(
        transition,
        {"high": True, "low": True},
        {},
        {"action:high-output": True},
    ) == ("high",)


def test_activation_inputs_are_exact_and_extend_region_order_deterministically() -> None:
    bit = BitType()
    output = StateResource(
        "output:y", "y", StateResourceKind.OUTPUT, bit, "clk"
    )
    action = _action(
        "action:y",
        output,
        StateActionKind.OUTPUT_WRITE,
        (Constant(1, bit),),
        "group:r",
        InputRef("enabled", bit),
    )
    group = ActionGroup(
        "group:r", "r", Constant(1, bit), (action,)
    )
    transition = ResolvedTransition(
        "transition:regions", "clk", "rst", (output,), (group,), ()
    )

    with pytest.raises(ValueError, match="requires action activation 'action:y'"):
        select_action_groups(transition, {"r": True}, {})
    with pytest.raises(ValueError, match="unknown action activation 'other'"):
        select_action_groups(
            transition, {"r": True}, {}, {"action:y": True, "other": False}
        )

    regions = selection_regions(transition, "r")
    # With no FIFO, dimensions are the rule guard followed by action
    # unique activation predicates in deterministic first-use order.
    for guard, active in product((False, True), repeat=2):
        represented = any(
            all(
                wanted is None or wanted == actual
                for wanted, actual in zip(region, (guard, active), strict=True)
            )
            for region in regions
        )
        assert represented == (guard and active)


def test_sibling_effects_share_one_scheduler_activation_dimension() -> None:
    bit = BitType()
    resources = tuple(
        StateResource(
            f"output:y{index}", f"y{index}", StateResourceKind.OUTPUT,
            bit, "clk",
        )
        for index in range(12)
    )
    selected = InputRef("selected", bit)
    actions = tuple(
        _action(
            f"action:y{index}", resource, StateActionKind.OUTPUT_WRITE,
            (Constant(1, bit),), "group:r", selected,
        )
        for index, resource in enumerate(resources)
    )
    transition = ResolvedTransition(
        "transition:shared-activation", "clk", "rst", resources,
        (ActionGroup("group:r", "r", Constant(1, bit), actions),), (),
    )

    assert len(conditional_actions(transition)) == 12
    assert conditional_activation_predicates(transition) == (selected,)
    regions = selection_regions(transition, "r")
    # One guard axis plus one shared branch-predicate axis.  A per-effect
    # implementation would expose thirteen dimensions and enumerate 4096
    # impossible combinations before minimization.
    assert all(len(region) == 2 for region in regions)
    assert regions == ((True, True),)


def _canonical_fixture():
    return analyze(parse("""
module ConditionalCanonical {
    clock clk reset rst
    in enable, select : bit
    in byte_value : u8
    out event : bit
    out byte_copy : u8
    reg state : bit = 0

    update: when enable {
        when select {
            event <- 1
            state <- 1
        }
    }

    byte_copy = byte_value
}
"""))


def test_conditional_rule_and_output_effect_round_trip_canonically() -> None:
    module = _canonical_fixture()
    canonical = lower(module)
    restored = restore(canonical)

    assert restored == module
    assert canonical.rules[0].actions[0].activation is not None
    transition = canonical.resolved_transition
    assert transition is not None
    assert [action.activation is not None for action in transition.action_groups[0].actions] == [
        True,
        True,
    ]


def test_canonical_restoration_rejects_non_bit_activation_and_bad_output_link() -> None:
    canonical = lower(_canonical_fixture())
    # Reuse the ordinary u8 assignment node as a deliberately malformed
    # activation predicate.
    bad_activation = canonical.assignments[0].expression
    rule = canonical.rules[0]
    bad_rule = replace(
        rule,
        actions=(
            replace(rule.actions[0], activation=bad_activation),
            *rule.actions[1:],
        ),
    )
    with pytest.raises(
        CanonicalizationError,
        match="rule 'update' action activation must have type bit",
    ):
        restore(replace(canonical, rules=(bad_rule,)))

    transition = canonical.resolved_transition
    assert transition is not None
    group = transition.action_groups[0]
    output_index = next(
        index
        for index, action in enumerate(group.actions)
        if action.kind is StateActionKind.OUTPUT_WRITE
    )
    output_action = group.actions[output_index]
    wrong_kind = replace(output_action, kind=StateActionKind.REGISTER_WRITE)
    wrong_kind_actions = list(group.actions)
    wrong_kind_actions[output_index] = wrong_kind
    malformed_transition = replace(
        transition,
        action_groups=(replace(
            group,
            actions=tuple(wrong_kind_actions),
        ),),
    )
    with pytest.raises(
        CanonicalizationError,
        match="output resource has a non-output action kind",
    ):
        restore(replace(canonical, resolved_transition=malformed_transition))

    mismatched_activation = replace(
        output_action,
        activation=None,
    )
    mismatched_actions = list(group.actions)
    mismatched_actions[output_index] = mismatched_activation
    malformed_transition = replace(
        transition,
        action_groups=(replace(
            group,
            actions=tuple(mismatched_actions),
        ),),
    )
    with pytest.raises(
        CanonicalizationError,
        match="effects do not match its typed rule",
    ):
        restore(replace(canonical, resolved_transition=malformed_transition))


def test_typed_activation_requires_exact_bit() -> None:
    bit = BitType()
    output = StateResource(
        "output:y", "y", StateResourceKind.OUTPUT, bit, "clk"
    )
    with pytest.raises(ValueError, match="state-action activation must have type bit"):
        _action(
            "action:y",
            output,
            StateActionKind.OUTPUT_WRITE,
            (Constant(1, bit),),
            "group:r",
            InputRef("not_bit", UIntType(2)),
        )
    with pytest.raises(ValueError, match="rule-action activation must have type bit"):
        NextAssignment(
            Port(PortDirection.OUTPUT, "y", bit),
            Constant(1, bit),
            InputRef("not_bit", UIntType(2)),
        )


def test_output_resource_is_not_a_protocol_endpoint() -> None:
    canonical = lower(_canonical_fixture())
    event = next(port for port in canonical.ports if port.name == "event")
    assert event.direction is PortDirection.OUTPUT
    assert event.protocol is InterfaceProtocol.WIRE

    protocol_event = replace(event, protocol=InterfaceProtocol.READY_VALID)
    malformed = replace(
        canonical,
        ports=tuple(
            protocol_event if port.name == "event" else port
            for port in canonical.ports
        ),
    )
    with pytest.raises(
        CanonicalizationError,
        match="output action links to a non-output resource",
    ):
        restore(malformed)
