"""ZTPU first-fault contract for nested sequential ``when`` action trees."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path

from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit_formal_artifact
from zlang.formal import build_recursive_formal_design
from zlang.opt import OptimizationStage, lower, restore
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "ztpu_first_fault_nested_when.zhl"


@dataclass(frozen=True)
class _State:
    valid: int
    overflow: int
    sticky: int


def _nested_transition(
    state: _State,
    *,
    fault: int,
    clear_valid: int,
    clear_overflow: int,
    code: int,
) -> _State:
    """Independent statement-level reading of the intended priority tree."""

    if fault:
        if state.valid:
            return _State(1, 1, state.sticky)
        return _State(1, 0, code)
    if clear_valid:
        return _State(0, 0, state.sticky)
    if clear_overflow:
        return _State(state.valid, 0, state.sticky)
    return state


def _flat_transition(
    state: _State,
    *,
    fault: int,
    clear_valid: int,
    clear_overflow: int,
    code: int,
) -> _State:
    """Independent equations used by the pre-ZL-016 source workaround."""

    capture = fault & (1 - state.valid)
    valid = 1 if fault else (0 if clear_valid else state.valid)
    overflow = (
        state.valid
        if fault
        else (0 if (clear_valid | clear_overflow) else state.overflow)
    )
    sticky = code if capture else state.sticky
    return _State(valid, overflow, sticky)


def _compile(top: str):
    return compile_source(
        FIXTURE.read_text(),
        top=top,
        include_clash=False,
        source_unit="tests/fixtures/ztpu_first_fault_nested_when.zhl",
    ).ir


def test_nested_priority_tree_and_flat_equations_are_exhaustively_equivalent() -> None:
    # Include unreachable pre-states as well as every four-bit old/new record.
    # This establishes equality of the complete one-edge transition relation,
    # not merely the states reached by the directed regression below.
    for valid, overflow, sticky, fault, clear_valid, clear_overflow, code in product(
        range(2), range(2), range(16), range(2), range(2), range(2), range(16)
    ):
        state = _State(valid, overflow, sticky)
        inputs = {
            "fault": fault,
            "clear_valid": clear_valid,
            "clear_overflow": clear_overflow,
            "code": code,
        }
        assert _nested_transition(state, **inputs) == _flat_transition(state, **inputs)


def test_nested_when_matches_flat_reference_and_exact_first_fault_trace() -> None:
    nested = _compile("ZtpuFirstFaultNestedWhen")
    flat = _compile("ZtpuFirstFaultFlatReference")

    vectors = [
        {"fault": 0, "clear_valid": 0, "clear_overflow": 0, "code": 0},
        {"fault": 1, "clear_valid": 0, "clear_overflow": 0, "code": 10},
        {"fault": 0, "clear_valid": 0, "clear_overflow": 0, "code": 0},
        {"fault": 1, "clear_valid": 0, "clear_overflow": 0, "code": 11},
        {"fault": 0, "clear_valid": 0, "clear_overflow": 1, "code": 0},
        {"fault": 1, "clear_valid": 1, "clear_overflow": 1, "code": 12},
        {"fault": 0, "clear_valid": 1, "clear_overflow": 0, "code": 0},
        {"fault": 1, "clear_valid": 0, "clear_overflow": 1, "code": 13},
        {"fault": 1, "clear_valid": 0, "clear_overflow": 0, "code": 14},
        {"fault": 0, "clear_valid": 0, "clear_overflow": 0, "code": 0},
    ]
    reset = [True, False, False, False, False, False, False, False, False, False]
    expected = [
        {"valid": 0, "overflow": 0, "sticky": 0},
        {"valid": 0, "overflow": 0, "sticky": 0},
        {"valid": 1, "overflow": 0, "sticky": 10},
        {"valid": 1, "overflow": 0, "sticky": 10},
        {"valid": 1, "overflow": 1, "sticky": 10},
        {"valid": 1, "overflow": 0, "sticky": 10},
        {"valid": 1, "overflow": 1, "sticky": 10},
        {"valid": 0, "overflow": 0, "sticky": 10},
        {"valid": 1, "overflow": 0, "sticky": 13},
        {"valid": 1, "overflow": 1, "sticky": 13},
    ]

    nested_trace = simulate_cycles(nested, vectors, reset=reset)
    flat_trace = simulate_cycles(flat, vectors, reset=reset)
    assert nested_trace == flat_trace == expected


def test_nested_when_is_deterministic_and_canonical_round_trip_is_lossless() -> None:
    first = _compile("ZtpuFirstFaultNestedWhen")
    second = _compile("ZtpuFirstFaultNestedWhen")
    assert first == second
    canonical = lower(first, stage=OptimizationStage.HIGH_LEVEL)
    assert restore(canonical) == first


def test_nested_when_keeps_one_existing_rule_fire_observation() -> None:
    module = _compile("ZtpuFirstFaultNestedWhen")
    design = build_recursive_formal_design(module)
    rule_bindings = tuple(
        item for item in design.bindings
        if item.ref.local_semantic_id.startswith("rule:")
    )
    assert len(rule_bindings) == 1
    assert rule_bindings[0].ref.local_semantic_id.endswith(".fire")

    artifact = emit_formal_artifact(module, design)
    physical = tuple(
        item for item in artifact.recursive_bindings
        if item.local_semantic_id.startswith("rule:")
    )
    assert len(physical) == 1
    assert physical[0].physical_available
    assert physical[0].formal_observation_token is not None
