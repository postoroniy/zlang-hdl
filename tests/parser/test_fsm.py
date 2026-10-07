from __future__ import annotations

import pytest

from zlang.ast import (
    FsmDecl,
    FsmStateDecl,
    FsmTransitionDecl,
    NameExpr,
    NextAssignment,
    TypeName,
)
from zlang.parser import ParseError, parse


SOURCE = """
enum TxPhase { Idle Header Payload Done }
module Controller {
    clock clk
    reset rst
    in command : bit
    in header_ready : bit
    in abort : bit
    in last : bit
    reg remaining : u8 = 0
    fsm phase : TxPhase = Idle {
        Idle {
            when command -> Header { remaining <- 7 }
        }
        Header {
            when header_ready -> Payload {}
        }
        Payload {
            priority {
                when abort -> Idle { remaining <- 0 }
                when last -> Done {}
            }
        }
        Done { -> Idle {} }
    }
}
"""


def test_concise_fsm_preserves_syntax_structure_and_spans() -> None:
    module = parse(SOURCE)
    assert len(module.fsms) == 1
    fsm = module.fsms[0]
    assert isinstance(fsm, FsmDecl)
    assert fsm.name == "phase"
    assert fsm.type_name == TypeName("TxPhase")
    assert fsm.initial_member == "Idle"
    assert tuple(state.member for state in fsm.states) == (
        "Idle", "Header", "Payload", "Done",
    )
    assert fsm.origin is not None
    idle = fsm.states[0]
    assert isinstance(idle, FsmStateDecl)
    assert not idle.priority and not idle.hold
    assert len(idle.transitions) == 1
    transition = idle.transitions[0]
    assert isinstance(transition, FsmTransitionDecl)
    assert isinstance(transition.guard, NameExpr)
    assert transition.target == "Header"
    assert len(transition.actions) == 1
    assert isinstance(transition.actions[0], NextAssignment)
    assert transition.actions[0].target == "remaining"
    payload = fsm.states[2]
    assert payload.priority
    assert tuple(item.target for item in payload.transitions) == ("Idle", "Done")
    done = fsm.states[3]
    assert not done.hold
    assert done.transitions[0].guard is None


def test_hold_state_and_unconditional_transition_are_distinct() -> None:
    module = parse(
        "enum E { A B } module M { clock c reset r "
        "fsm s:E=A { A { -> B {} } B { hold } } }"
    )
    first, second = module.fsms[0].states
    assert first.transitions[0].guard is None
    assert not first.hold
    assert second.hold
    assert not second.transitions


@pytest.mark.parametrize(
    "body",
    (
        "A {}",
        "A { when x -> B {} when y -> B {} }",
        "A { priority {} }",
    ),
)
def test_malformed_fsm_state_body_is_rejected_by_grammar(body: str) -> None:
    source = (
        "enum E { A B } module M { clock c reset r in x:bit in y:bit "
        f"fsm s:E=A {{ {body} B {{ hold }} }} }}"
    )
    with pytest.raises(ParseError, match="syntax error"):
        parse(source)
