"""Composed IEEE IFFT64, natural-order reorder, and CP validation."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from tests.integration.test_80211a_ifft_library import (
    _exact_ifft64_frame,
)
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file
from zlang.opt import OptimizationStage, lower, restore
from zlang.ir.types import EnumType
from zlang.simulate import _PersistentStorageSimulationState, simulate_cycles
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "ifft.zl"
)
PLAIN_TOP = "IeeeIFFT64"
RAW_TOP = "IeeeFramedIFFT64Raw"


def _input_strip_module():
    root = compile_file(SOURCE, top=RAW_TOP, include_clash=False).ir
    pending = [root]
    while pending:
        module = pending.pop()
        if module.name == "IeeeIFFTInputStrip":
            return module
        pending.extend(module.children)
    raise AssertionError("IFFT hierarchy has no IeeeIFFTInputStrip")


def _strip_cycle(
    *,
    valid: int = 0,
    last: int = 0,
    ready: int = 1,
    real: int = 123,
    imaginary: int = -45,
) -> dict[str, object]:
    return {
        "input": {
            "payload": {
                "data": {"re": real, "im": imaginary},
                "meta": {
                    "rate": 1,
                    "symbol_index": 0,
                    "symbol_first": 1,
                    "symbol_last": last,
                },
                "first": 1,
                "last": last,
            },
            "valid": valid,
        },
        "allow": 1,
        "output": {"ready": ready},
    }


def _reverse6(value: int) -> int:
    return int(f"{value:06b}"[::-1], 2)


def _expected_cp(sample: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    dif = _exact_ifft64_frame([sample] * 64)
    natural = tuple(dif[_reverse6(index)] for index in range(64))
    return (*natural[48:64], *natural)


def _plain_cycles(count: int) -> tuple[list[dict[str, object]], list[bool]]:
    # A constant valid payload is protocol-stable through arbitrary upstream
    # backpressure.  It is also a useful non-zero transform witness: only the
    # time-domain impulse may remain after exact IFFT scaling.
    cycles = [
        {
            "input": {
                "payload": {"re": 512, "im": -256},
                "valid": 1,
            },
            "output": {"ready": int(index % 11 not in (4, 5, 9))},
        }
        for index in range(count)
    ]
    resets = [index in (0, 260) for index in range(count)]
    return cycles, resets


def _raw_input_cycle(
    ordinal: int = 0,
    *,
    valid: int = 0,
    ready: int = 1,
    symbol_index: int = 7,
) -> dict[str, object]:
    return {
        "input": {
            "payload": {
                "data": {"re": 512, "im": -256},
                "meta": {
                    "rate": 1,
                    "symbol_index": symbol_index,
                    "symbol_first": int(ordinal == 0),
                    "symbol_last": int(ordinal == 63),
                },
                "first": int(ordinal == 0),
                "last": int(ordinal == 63),
            },
            "valid": valid,
        },
        "output": {"ready": ready},
    }


def test_ieee_ifft_hierarchy_and_canonical_round_trip() -> None:
    plain = compile_file(SOURCE, top=PLAIN_TOP, include_clash=False).ir
    assert [child.name for child in plain.children] == [
        "IFFT64DIFExactChain",
        "IFFT64ReorderCP",
    ]
    assert restore(lower(plain, stage=OptimizationStage.HIGH_LEVEL)) == plain

    raw = compile_file(SOURCE, top=RAW_TOP, include_clash=False).ir
    assert [child.name for child in raw.children] == [
        "IeeeIFFTFramedInputBoundary",
        "IeeeFramedIFFT64",
        "IeeeIFFTFramedOutputBoundary",
    ]
    framed = raw.children[1]
    assert [fifo.name for fifo in framed.fifos] == ["metadata"]
    assert framed.fifos[0].depth == 8
    assert restore(lower(raw, stage=OptimizationStage.HIGH_LEVEL)) == raw


def test_input_strip_concise_fsm_lowers_to_enum_register_and_rules() -> None:
    strip = _input_strip_module()

    assert tuple(register.name for register in strip.registers) == (
        "flush_remaining",
        "phase",
    )
    phase = strip.registers[1]
    assert isinstance(phase.type, EnumType)
    assert phase.type.name == "IeeeIFFTStripPhase"
    assert phase.type.members == ("Data", "Flush")
    assert phase.initial.value == 0
    assert len(strip.rules) == 3
    assert all(rule.name.startswith("__fsm_") for rule in strip.rules)
    assert all(
        tuple(action.target.name for action in rule.actions)
        == ("phase", "flush_remaining")
        for rule in strip.rules
    )
    assert len(strip.rule_priorities) == 1
    priority = strip.rule_priorities[0]
    assert priority.higher == strip.rules[1].name
    assert priority.lower == strip.rules[2].name
    assert restore(lower(strip, stage=OptimizationStage.HIGH_LEVEL)) == strip


def test_input_strip_fsm_flushes_64_zeroes_under_stalls_and_reset() -> None:
    strip = _input_strip_module()
    state = _PersistentStorageSimulationState(strip)

    state.step(_strip_cycle(), True)
    accepted = state.step(_strip_cycle(valid=1, last=1), False)
    assert accepted["input"]["transfer"] == 1
    assert accepted["output"]["transfer"] == 1
    assert accepted["output"]["payload"] == {"re": 123, "im": -45}

    flush_transfers = 0
    for cycle in range(100):
        ready = int(cycle % 4 != 1)
        result = state.step(_strip_cycle(ready=ready), False)
        if result["output"]["valid"]:
            assert result["input"]["ready"] == 0
            assert result["output"]["payload"] == {"re": 0, "im": 0}
        flush_transfers += int(result["output"]["transfer"])
        if flush_transfers == 64:
            break
    assert flush_transfers == 64
    idle = state.step(_strip_cycle(), False)
    assert idle["output"]["valid"] == 0
    assert idle["input"]["ready"] == 1
    assert state.register_state == {"flush_remaining": 0, "phase": 0}

    # A new packet epoch may begin immediately.  Reset part-way through its
    # flush discards the remaining zero frame and restores the Data state.
    state.step(_strip_cycle(valid=1, last=1), False)
    for _ in range(7):
        result = state.step(_strip_cycle(), False)
        assert result["output"]["transfer"] == 1
    assert state.register_state == {"flush_remaining": 57, "phase": 1}
    state.step(_strip_cycle(), True)
    after_reset = state.step(_strip_cycle(), False)
    assert after_reset["output"]["valid"] == 0
    assert after_reset["input"]["ready"] == 1
    assert state.register_state == {"flush_remaining": 0, "phase": 0}


def test_ieee_ifft_persistent_nested_simulation_matches_exact_cp() -> None:
    module = compile_file(SOURCE, top=PLAIN_TOP, include_clash=False).ir
    cycles, resets = _plain_cycles(900)
    actual = simulate_cycles(module, cycles, reset=resets)
    after_reset = actual[261:]
    transferred = tuple(
        (
            int(item["output"]["payload"]["re"]),
            int(item["output"]["payload"]["im"]),
        )
        for item in after_reset
        if item["output"]["transfer"]
    )
    expected = _expected_cp((512, -256))
    assert len(transferred) >= 80
    assert transferred[:80] == expected

    # The final conversion is the only narrowing boundary.  A constant set of
    # frequency bins yields one bounded impulse and otherwise exact zeroes.
    assert any(value != (0, 0) for value in expected)
    assert sum(value != (0, 0) for value in expected[16:]) == 1


def test_framed_ifft_flushes_finite_packet_and_preserves_metadata() -> None:
    module = compile_file(SOURCE, top=RAW_TOP, include_clash=False).ir
    cycles = [_raw_input_cycle()]
    resets = [True]

    # An incomplete pre-reset symbol must never escape the new protocol epoch.
    for ordinal in range(20):
        cycles.append(_raw_input_cycle(ordinal, valid=1))
        resets.append(False)
    cycles.append(_raw_input_cycle())
    resets.append(True)

    for ordinal in range(64):
        cycles.append(_raw_input_cycle(ordinal, valid=1))
        resets.append(False)
    for index in range(650):
        cycles.append(_raw_input_cycle(ready=int(index % 9 not in (2, 3))))
        resets.append(False)

    actual = simulate_cycles(module, cycles, reset=resets)
    transferred = [
        item["output"]["payload"]
        for item in actual
        if item["output"]["transfer"]
    ]
    assert len(transferred) == 80
    assert tuple(
        (int(item["data"]["re"]), int(item["data"]["im"]))
        for item in transferred
    ) == _expected_cp((512, -256))
    assert [item["meta"]["rate"] for item in transferred] == [1] * 80
    assert [item["meta"]["symbol_index"] for item in transferred] == [7] * 80
    assert [item["meta"]["symbol_first"] for item in transferred] == [
        1,
        *([0] * 79),
    ]
    assert [item["meta"]["symbol_last"] for item in transferred] == [
        *([0] * 79),
        1,
    ]
    assert [item["first"] for item in transferred] == [1, *([0] * 79)]
    assert [item["last"] for item in transferred] == [*([0] * 79), 1]


def test_ieee_ifft_direct_sv_artifact_is_deterministic_and_strict() -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is unavailable")
    module = compile_file(SOURCE, top=RAW_TOP, include_clash=False).ir
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.artifact_hash == first.artifact_hash
    assert restored.bindings == first.bindings
    bindings = {item.semantic_signal_id: item for item in first.bindings}
    for semantic_id in (
        "port:input.payload.data.re",
        "port:input.payload.data.im",
        "port:output.payload.data.re",
        "port:output.payload.data.im",
    ):
        assert bindings[semantic_id].physical_available
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / f"{RAW_TOP}.sv"
        rtl.write_text(first.text)
        lint_with_verilator((rtl,), RAW_TOP)


def test_ieee_ifft_real_clash_generation_is_bounded() -> None:
    clash = find_clash_executable()
    if clash is None or shutil.which("verilator") is None:
        pytest.skip("Clash and Verilator are required")
    module = compile_file(SOURCE, top=PLAIN_TOP, include_clash=False).ir
    artifact = emit_clash_artifact(module)
    assert len(artifact.text) < 180_000
    with tempfile.TemporaryDirectory() as temporary:
        rtl = generate_verilog(
            artifact.text,
            PLAIN_TOP,
            Path(temporary),
            clash,
            companions=artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(module),
        )
        # Clash 1.11's romFile implementation widens the Verilog selector to
        # host Int.  Arithmetic/payload warnings are not waived.
        lint = subprocess.run(
            (
                "verilator",
                "--lint-only",
                "-Wno-WIDTHTRUNC",
                "--top-module",
                PLAIN_TOP,
                *(str(path) for path in rtl),
            ),
            capture_output=True,
            text=True,
        )
        assert lint.returncode == 0, lint.stderr or lint.stdout
