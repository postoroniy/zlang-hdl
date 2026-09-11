from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.opt import OptimizationStage, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate_cycles


CONCISE = """
enum TxPhase { Idle Header Payload Done }
module Controller {
    clock clk
    reset rst
    in command : bit
    in header_ready : bit
    in abort : bit
    in last : bit
    out state_out : TxPhase
    out remaining_out : u8
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
    state_out = phase
    remaining_out = remaining
}
"""


VERBOSE = """
enum TxPhase { Idle Header Payload Done }
module Controller {
    clock clk
    reset rst
    in command : bit
    in header_ready : bit
    in abort : bit
    in last : bit
    out state_out : TxPhase
    out remaining_out : u8
    reg remaining : u8 = 0
    reg phase : TxPhase = TxPhase.Idle
    rule idle_to_header when (phase == TxPhase.Idle) & command {
        phase <- TxPhase.Header
        remaining <- 7
    }
    rule header_to_payload when (phase == TxPhase.Header) & header_ready {
        phase <- TxPhase.Payload
    }
    rule payload_abort when (phase == TxPhase.Payload) & abort {
        phase <- TxPhase.Idle
        remaining <- 0
    }
    rule payload_last when (phase == TxPhase.Payload) & last {
        phase <- TxPhase.Done
    }
    rule done_to_idle when phase == TxPhase.Done {
        phase <- TxPhase.Idle
    }
    priority payload_abort > payload_last
    state_out = phase
    remaining_out = remaining
}
"""


def _trace(source: str) -> list[dict[str, int]]:
    module = compile_source(source).ir
    return simulate_cycles(
        module,
        [
            {"command": 0, "header_ready": 0, "abort": 0, "last": 0},
            {"command": 1, "header_ready": 0, "abort": 0, "last": 0},
            {"command": 0, "header_ready": 1, "abort": 0, "last": 0},
            {"command": 0, "header_ready": 0, "abort": 0, "last": 1},
            {"command": 0, "header_ready": 0, "abort": 0, "last": 0},
            {"command": 1, "header_ready": 0, "abort": 0, "last": 0},
            {"command": 0, "header_ready": 1, "abort": 0, "last": 0},
            {"command": 0, "header_ready": 0, "abort": 1, "last": 1},
        ],
        reset=[True, False, False, False, False, False, False, False],
    )


def test_concise_fsm_lowers_before_typed_ir_and_matches_verbose_cycles() -> None:
    concise = compile_source(CONCISE).ir
    verbose = compile_source(VERBOSE).ir
    assert tuple(register.name for register in concise.registers) == (
        "remaining", "phase",
    )
    assert len(concise.rules) == len(verbose.rules) == 5
    assert len(concise.rule_priorities) == len(verbose.rule_priorities) == 1
    assert all(rule.name.startswith("__fsm_") for rule in concise.rules)
    assert _trace(CONCISE) == _trace(VERBOSE)


def test_concise_fsm_is_deterministic_and_canonical_round_trip_is_lossless() -> None:
    first = compile_source(CONCISE).ir
    second = compile_source(CONCISE).ir
    assert tuple(rule.name for rule in first.rules) == tuple(
        rule.name for rule in second.rules
    )
    canonical = lower(first, stage=OptimizationStage.HIGH_LEVEL)
    assert restore(canonical) == first


@pytest.mark.parametrize(
    "source,message",
    (
        (
            "module M { clock c reset r fsm s:u2=Idle { Idle { hold } } }",
            "state type must be an enum",
        ),
        (
            "enum E { A B } module M { clock c reset r "
            "fsm s:E=A { A { hold } } }",
            "missing enum state",
        ),
        (
            "enum E { A B } module M { clock c reset r "
            "fsm s:E=A { A { hold } A { hold } B { hold } } }",
            "defines state 'A' more than once",
        ),
        (
            "enum E { A B } module M { clock c reset r "
            "fsm s:E=A { A { hold } C { hold } } }",
            "state 'C' is not a member",
        ),
        (
            "enum E { A B } module M { clock c reset r "
            "fsm s:E=C { A { hold } B { hold } } }",
            "initial state 'C'",
        ),
        (
            "enum E { A B } module M { clock c reset r "
            "fsm s:E=A { A { -> C {} } B { hold } } }",
            "transition target 'C'",
        ),
        (
            "enum E { A B } module M { clock c reset r in x:bit "
            "fsm s:E=A { A { priority { when x -> B {} } } B { hold } } }",
            "requires at least two transitions",
        ),
        (
            "enum E { A B } module M { clock c reset r in x:bit "
            "fsm s:E=A { A { priority { -> B {} when x -> A {} } } B { hold } } }",
            "priority transitions require explicit when guards",
        ),
        (
            "enum E { A B } module M { clock c reset r in x:bit "
            "fsm s:E=A { A { when x -> B { s <- E.A } } B { hold } } }",
            "cannot be written explicitly inside a transition",
        ),
        (
            "enum E { A B } module M { clock c reset r in x:bit "
            "fsm s:E=A { A { when x -> B {} } B { hold } } "
            "rule bad when x { s <- E.A } }",
            "cannot be written outside its transitions",
        ),
    ),
)
def test_concise_fsm_diagnostics(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        analyze(parse(source))
