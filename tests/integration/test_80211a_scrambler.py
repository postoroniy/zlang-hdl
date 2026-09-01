"""Independent randomized validation of the canonical IEEE scrambler unit."""

from __future__ import annotations

from pathlib import Path
import random

import pytest

from zlang.compiler import compile_file
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/projects/80211a_transmitter/src/controller.zl"
TOP = "IeeePacketFramerScrambler24"
SEED = 0x4B


def _scramble_word(data: int, state: int) -> tuple[int, int]:
    if not 0 <= data < 1 << 24:
        raise ValueError("scrambler data must fit 24 bits")
    if not 0 <= state < 1 << 7:
        raise ValueError("scrambler state must fit 7 bits")
    output = 0
    for index in range(24):
        feedback = ((state >> 0) ^ (state >> 3)) & 1
        output |= (((data >> index) & 1) ^ feedback) << index
        state = ((feedback << 6) | (state >> 1)) & 0x7F
    return output, state


def _scrambler_module():
    owner = compile_file(SOURCE, top=TOP, include_clash=False).ir
    pending = [owner]
    while pending:
        module = pending.pop()
        if module.name == "IeeeDataScrambler24":
            return module
        pending.extend(module.children)
    raise AssertionError("canonical hierarchy has no IeeeDataScrambler24")


def _beat(
    data: int,
    *,
    first: int = 0,
    last: int = 0,
    rate: int = 1,
    tail_mask: int = 0,
) -> dict[str, object]:
    return {
        "data": data,
        "meta": {
            "rate": rate,
            "valid_bytes": 3,
            "tail_mask": tail_mask,
            "symbol_first": first,
            "symbol_last": last,
        },
        "first": first,
        "last": last,
    }


def _cycle(
    payload: dict[str, object],
    *,
    valid: int,
    ready: int,
) -> dict[str, object]:
    return {
        "input": {"payload": payload, "valid": valid},
        "output": {"ready": ready},
    }


def _expected(beats: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    state = SEED
    result = []
    for beat in beats:
        if beat["first"]:
            state = SEED
        data, state = _scramble_word(int(beat["data"]), state)
        copied = dict(beat)
        copied["meta"] = dict(beat["meta"])
        copied["data"] = data & (~int(copied["meta"]["tail_mask"]) & 0xFFFFFF)
        result.append(copied)
    return result


def _transferred(results: list[dict[str, object]]) -> list[object]:
    return [
        cycle["output"]["payload"]
        for cycle in results
        if cycle["output"]["transfer"]
    ]


def test_scrambler_integer_oracle_anchors_and_input_bounds() -> None:
    first, first_state = _scramble_word(0, SEED)
    second, second_state = _scramble_word(0, first_state)
    assert (first, first_state) == (0x1FC762, 0x0F)
    assert (second, second_state) == (0x1269EE, 0x09)
    with pytest.raises(ValueError, match="24 bits"):
        _scramble_word(1 << 24, SEED)
    with pytest.raises(ValueError, match="7 bits"):
        _scramble_word(0, 1 << 7)


def test_scrambler_random_multiword_oracle_is_deterministic() -> None:
    generator = random.Random(0x80211A)
    beats = tuple(
        _beat(
            generator.randrange(1 << 24),
            first=int(index == 0 or index % 13 == 0),
            last=int(index == 95),
            rate=generator.choice((1, 2, 4)),
        )
        for index in range(96)
    )
    assert _expected(beats) == _expected(beats)
    assert len(_expected(beats)) == 96


def test_scrambler_simulator_matches_random_oracle_at_ii_one() -> None:
    generator = random.Random(0x5C4A6B1E)
    beats = tuple(
        _beat(
            generator.randrange(1 << 24),
            first=int(index == 0 or index % 13 == 0),
            last=int(index == 63),
            rate=generator.choice((1, 2, 4)),
            tail_mask=(0x3F << 18) if index == 63 else 0,
        )
        for index in range(64)
    )
    idle = _beat(0)
    cycles = [_cycle(idle, valid=0, ready=1)]
    resets = [True]
    for index, beat in enumerate(beats):
        if index % 11 == 5:
            cycles.append(_cycle(idle, valid=0, ready=1))
            resets.append(False)
        cycles.append(_cycle(beat, valid=1, ready=1))
        resets.append(False)
    cycles.extend((_cycle(idle, valid=0, ready=1),) * 2)
    resets.extend((False, False))

    results = simulate_cycles(_scrambler_module(), cycles, reset=resets)
    assert sum(cycle["input"]["transfer"] for cycle in results) == len(beats)
    assert _transferred(results) == _expected(beats)


def test_scrambler_stall_and_midstream_reset_start_a_new_epoch() -> None:
    idle = _beat(0)
    first = _beat(0, first=1)
    second = _beat(0)
    discarded = _beat(0xA5A5A5)
    fresh = _beat(0, first=1, last=1)
    cycles = [
        _cycle(idle, valid=0, ready=0),
        _cycle(first, valid=1, ready=0),
        _cycle(second, valid=1, ready=0),
        _cycle(idle, valid=0, ready=0),
        _cycle(idle, valid=0, ready=1),
        _cycle(idle, valid=0, ready=1),
        _cycle(discarded, valid=1, ready=0),
        _cycle(idle, valid=0, ready=0),
        _cycle(idle, valid=0, ready=0),
        _cycle(fresh, valid=1, ready=0),
        _cycle(idle, valid=0, ready=1),
        _cycle(idle, valid=0, ready=1),
    ]
    resets = [True, False, False, False, False, False, False, False, True]
    resets.extend((False,) * (len(cycles) - len(resets)))

    results = simulate_cycles(_scrambler_module(), cycles, reset=resets)
    assert results[2]["output"]["payload"] == _expected((first,))[0]
    assert results[2]["output"]["payload"] == results[3]["output"]["payload"]
    assert results[4]["output"]["transfer"] == 1
    assert results[5]["output"]["transfer"] == 1
    assert results[8]["output"]["valid"] == 0
    assert results[9]["input"]["transfer"] == 1
    assert results[10]["output"]["payload"] == _expected((fresh,))[0]
    assert results[10]["output"]["transfer"] == 1
