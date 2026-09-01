"""Bounded backend-independent simulation of sequential RV hierarchy."""

from pathlib import Path
from statistics import median
from time import perf_counter

from zlang.compiler import compile_file, compile_source
from zlang.simulate import (
    _PersistentStorageSimulationState,
    simulate_cycles,
    simulate_storage_cycles,
)


ROOT = Path(__file__).resolve().parents[2]
FFT_SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zl"
PRODUCTION_IFFT_SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "ifft.zl"
)


NESTED_FIFO_SOURCE = """
module Leaf {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u8>

    fifo queue : fifo<u8,2>
    queue.data = input.payload
    queue.push = input.transfer
    queue.pop = output.transfer
    input.ready = queue.ready
    output.payload = queue.front
    output.valid = queue.valid
}

module Middle {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u8>
    inst leaf : Leaf
    connect input -> leaf.input
    connect leaf.output -> output
}

module NestedTop {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u8>
    inst middle : Middle
    connect input -> middle.input
    connect middle.output -> output
}
"""


STATEFUL_NESTED_SOURCE = """
module Tagger {
    in input : rv<u8>
    in tag : u8
    out output : rv<u9>
    output.payload = truncate<9>(
        extend<9>(input.payload) + extend<9>(tag)
    )
    output.valid = input.valid
    input.ready = output.ready
}

module StatefulMiddle {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u9>

    reg offset : u8 = 0
    inst tagger : Tagger { tag = offset }
    connect input -> tagger.input
    connect tagger.output -> output

    rule bump when input.transfer {
        offset <- truncate<8>(offset + 1)
    }
}

module StatefulNestedTop {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u9>
    inst middle : StatefulMiddle
    connect input -> middle.input
    connect middle.output -> output
}
"""


def _fft4():
    return compile_source(
        FFT_SOURCE.read_text(),
        top="FFT4SDFReference",
        include_clash=False,
    ).ir


def _cycle(real: int, imag: int, *, valid: int = 1, ready: int = 1):
    return {
        "input": {"payload": {"re": real, "im": imag}, "valid": valid},
        "output": {"ready": ready},
    }


def _transferred_payloads(results):
    return [
        item["output"]["payload"]
        for item in results
        if item["output"]["transfer"]
    ]


def test_two_stage_fft4_hierarchy_has_atomic_child_steps_and_exact_results() -> None:
    # One reset cycle, four frame samples, three accepted zeros to flush the
    # finite SDF stream, and two idle cycles to observe the registered tail.
    cycles = [
        _cycle(0, 0, valid=0),
        _cycle(1000, 200),
        _cycle(-300, 500),
        _cycle(700, -100),
        _cycle(-200, -400),
        _cycle(0, 0),
        _cycle(0, 0),
        _cycle(0, 0),
        _cycle(0, 0, valid=0),
        _cycle(0, 0, valid=0),
    ]

    results = simulate_cycles(
        _fft4(), cycles, reset=[True] + [False] * (len(cycles) - 1)
    )

    # The radix-2 DIF/SDF chain emits bins in bit-reversed order 0, 2, 1, 3.
    assert _transferred_payloads(results) == [
        {"re": 1200, "im": 200},
        {"re": 2200, "im": 0},
        {"re": 1200, "im": 400},
        {"re": -600, "im": 200},
    ]
    assert [
        index for index, item in enumerate(results) if item["output"]["transfer"]
    ] == [6, 7, 8, 9]


def test_two_stage_fft4_hierarchy_preserves_stalled_output_and_reset_epoch() -> None:
    # Discard a partial pre-reset frame, then run the same complete frame.  Two
    # downstream stall cycles begin when the first result becomes visible.
    cycles = [
        _cycle(10, 20),
        _cycle(30, 40),
        _cycle(0, 0, valid=0),
        _cycle(1000, 200),
        _cycle(-300, 500),
        _cycle(700, -100),
        _cycle(-200, -400),
        _cycle(0, 0),
        _cycle(0, 0, ready=0),
        _cycle(0, 0, ready=0),
        _cycle(0, 0),
        _cycle(0, 0),
        _cycle(0, 0, valid=0),
        _cycle(0, 0, valid=0),
        _cycle(0, 0, valid=0),
    ]
    resets = [False, False, True] + [False] * (len(cycles) - 3)

    results = simulate_cycles(_fft4(), cycles, reset=resets)

    assert results[8]["output"]["valid"] == 1
    assert results[9]["output"]["valid"] == 1
    assert results[8]["output"]["payload"] == results[9]["output"]["payload"]
    assert _transferred_payloads(results) == [
        {"re": 1200, "im": 200},
        {"re": 2200, "im": 0},
        {"re": 1200, "im": 400},
        {"re": -600, "im": 200},
    ]


def test_persistent_child_preview_and_step_match_storage_cycle_history() -> None:
    child = _fft4().children[0]
    cycles = [
        _cycle(0, 0, valid=0),
        _cycle(1000, 200),
        _cycle(-300, 500),
        _cycle(700, -100, ready=0),
        _cycle(700, -100, ready=0),
        _cycle(700, -100),
        _cycle(-200, -400),
        _cycle(0, 0),
    ]
    resets = [True] + [False] * (len(cycles) - 1)
    expected = [
        {
            "input": {"ready": 1, "transfer": 0},
            "output": {
                "payload": {"re": 0, "im": 0},
                "valid": 0,
                "transfer": 0,
            },
        },
        {
            "input": {"ready": 1, "transfer": 1},
            "output": {
                "payload": {"re": 0, "im": 0},
                "valid": 0,
                "transfer": 0,
            },
        },
        {
            "input": {"ready": 1, "transfer": 1},
            "output": {
                "payload": {"re": 0, "im": 0},
                "valid": 0,
                "transfer": 0,
            },
        },
        {
            "input": {"ready": 1, "transfer": 1},
            "output": {
                "payload": {"re": 0, "im": 0},
                "valid": 0,
                "transfer": 0,
            },
        },
        {
            "input": {"ready": 0, "transfer": 0},
            "output": {
                "payload": {"re": 1700, "im": 100},
                "valid": 1,
                "transfer": 0,
            },
        },
        {
            "input": {"ready": 1, "transfer": 1},
            "output": {
                "payload": {"re": 1700, "im": 100},
                "valid": 1,
                "transfer": 1,
            },
        },
        {
            "input": {"ready": 1, "transfer": 1},
            "output": {
                "payload": {"re": 400, "im": 400},
                "valid": 1,
                "transfer": 1,
            },
        },
        {
            "input": {"ready": 1, "transfer": 1},
            "output": {
                "payload": {"re": 300, "im": 300},
                "valid": 1,
                "transfer": 1,
            },
        },
    ]
    assert simulate_storage_cycles(child, cycles, reset=resets) == expected

    state = _PersistentStorageSimulationState(child)
    actual = []
    for inputs, reset_active in zip(cycles, resets, strict=True):
        first_preview = state.preview(inputs, reset_active)
        second_preview = state.preview(inputs, reset_active)
        committed = state.step(inputs, reset_active)
        assert first_preview == second_preview == committed
        actual.append(committed)

    assert actual == expected


def test_hierarchical_persistent_simulation_scales_linearly_with_cycles() -> None:
    module = _fft4()

    def elapsed(count: int) -> float:
        cycles = [_cycle(0, 0, valid=0) for _ in range(count)]
        resets = [True] + [False] * (count - 1)
        started = perf_counter()
        results = simulate_cycles(module, cycles, reset=resets)
        duration = perf_counter() - started
        assert len(results) == count
        return duration

    elapsed(8)  # Warm caches before measuring the scaling ratio.
    ratios = [elapsed(256) / elapsed(128) for _ in range(3)]

    assert median(ratios) <= 2.5, ratios


def test_nested_ready_valid_hierarchy_commits_leaf_state_once_per_cycle() -> None:
    module = compile_source(
        NESTED_FIFO_SOURCE,
        top="NestedTop",
        include_clash=False,
    ).ir
    cycles = [
        {"input": {"payload": 0, "valid": 0}, "output": {"ready": 1}},
        {"input": {"payload": 7, "valid": 1}, "output": {"ready": 0}},
        {"input": {"payload": 9, "valid": 1}, "output": {"ready": 0}},
        {"input": {"payload": 11, "valid": 1}, "output": {"ready": 0}},
        {"input": {"payload": 11, "valid": 1}, "output": {"ready": 1}},
        {"input": {"payload": 0, "valid": 0}, "output": {"ready": 1}},
        {"input": {"payload": 0, "valid": 0}, "output": {"ready": 1}},
    ]
    result = simulate_cycles(
        module,
        cycles,
        reset=[True, False, False, False, False, False, False],
    )

    assert [item["input"]["ready"] for item in result] == [0, 1, 1, 0, 1, 1, 1]
    assert [
        item["output"]["payload"]
        for item in result
        if item["output"]["transfer"]
    ] == [7, 9, 11]
    assert result[2]["output"] == {
        "payload": 7,
        "valid": 1,
        "transfer": 0,
    }
    assert result[3]["output"] == result[2]["output"]


def test_nested_ready_valid_hierarchy_scales_linearly_with_cycles() -> None:
    module = compile_source(
        NESTED_FIFO_SOURCE,
        top="NestedTop",
        include_clash=False,
    ).ir

    def elapsed(count: int) -> float:
        cycles = [
            {
                "input": {"payload": 0, "valid": 0},
                "output": {"ready": 1},
            }
            for _ in range(count)
        ]
        started = perf_counter()
        result = simulate_cycles(
            module,
            cycles,
            reset=[True] + [False] * (count - 1),
        )
        duration = perf_counter() - started
        assert len(result) == count
        return duration

    elapsed(8)
    ratios = [elapsed(256) / elapsed(128) for _ in range(3)]
    assert median(ratios) <= 2.5, ratios


def test_nested_hierarchy_parent_state_and_mixed_child_commit_atomically() -> None:
    nested = compile_source(
        STATEFUL_NESTED_SOURCE,
        top="StatefulNestedTop",
        include_clash=False,
    ).ir
    direct = compile_source(
        STATEFUL_NESTED_SOURCE,
        top="StatefulMiddle",
        include_clash=False,
    ).ir
    stimulus = (
        (0, 0, 1),
        (10, 1, 1),
        (20, 1, 0),
        (20, 1, 0),
        (20, 1, 1),
        (30, 1, 1),
        (0, 0, 1),
        (40, 1, 1),
    )
    cycles = [
        {
            "input": {"payload": payload, "valid": valid},
            "output": {"ready": ready},
        }
        for payload, valid, ready in stimulus
    ]
    resets = [True, False, False, False, False, False, True, False]
    result = simulate_cycles(nested, cycles, reset=resets)
    assert simulate_cycles(direct, cycles, reset=resets) == result

    # The tag is the pre-edge accepted-transfer count.  Two stalled previews
    # neither advance it nor alter the held payload; a mid-stream reset starts
    # the physical child/parent epoch together.
    assert [item["output"]["payload"] for item in result] == [
        0,
        10,
        21,
        21,
        21,
        32,
        0,
        40,
    ]
    assert result[2]["output"] == result[3]["output"]
    assert [item["input"]["transfer"] for item in result] == [
        0,
        1,
        0,
        0,
        1,
        1,
        0,
        1,
    ]


def test_ieee_ifft_nested_hierarchy_simulates_and_emits_one_cp_symbol() -> None:
    module = compile_file(
        PRODUCTION_IFFT_SOURCE,
        top="IeeeIFFT64",
        include_clash=False,
    ).ir
    zero = {"re": 0, "im": 0}
    cycles = [
        {
            "input": {"payload": zero, "valid": int(index != 0)},
            "output": {"ready": 1},
        }
        for index in range(256)
    ]
    result = simulate_cycles(
        module,
        cycles,
        reset=[index == 0 for index in range(len(cycles))],
    )
    transferred = [
        item["output"]["payload"]
        for item in result
        if item["output"]["transfer"]
    ]

    assert transferred == [zero] * 80
    assert next(
        index
        for index, item in enumerate(result)
        if item["output"]["transfer"]
    ) == 134
