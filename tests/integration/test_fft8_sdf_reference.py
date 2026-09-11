"""Bounded simulator validation for the three-stage FFT8 SDF reference.

The numerical oracle is deliberately independent of :mod:`zlang.simulate`.
It applies the fixed-point contract once at every D=4, D=2, and D=1 stage
boundary, matching the architectural quantization points in the ZLang source.
"""

from functools import lru_cache
from pathlib import Path

from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zhl"
SAMPLE_WIDTH = 18
SAMPLE_MIN = -(1 << (SAMPLE_WIDTH - 1))
SAMPLE_MAX = (1 << (SAMPLE_WIDTH - 1)) - 1
TWIDDLE_SCALE = 1 << 14
ZERO = {"re": 0, "im": 0}

TWIDDLES = {
    4: (
        (16384, 0),
        (11585, -11585),
        (0, -16384),
        (-11585, -11585),
    ),
    2: ((16384, 0), (0, -16384)),
    1: ((16384, 0),),
}


@lru_cache(maxsize=1)
def _fft8():
    return compile_source(
        SOURCE.read_text(), top="FFT8SDFReference"
    ).ir


def _saturate(value: int) -> int:
    return min(SAMPLE_MAX, max(SAMPLE_MIN, value))


def _nearest_even(numerator: int, denominator: int = TWIDDLE_SCALE) -> int:
    """Round an exact signed rational to the nearest integer, ties to even."""

    sign = -1 if numerator < 0 else 1
    quotient, remainder = divmod(abs(numerator), denominator)
    twice_remainder = remainder * 2
    if twice_remainder > denominator or (
        twice_remainder == denominator and quotient & 1
    ):
        quotient += 1
    return sign * quotient


def _add(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {
        "re": _saturate(left["re"] + right["re"]),
        "im": _saturate(left["im"] + right["im"]),
    }


def _subtract(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {
        "re": _saturate(left["re"] - right["re"]),
        "im": _saturate(left["im"] - right["im"]),
    }


def _multiply_twiddle(
    value: dict[str, int], twiddle: tuple[int, int]
) -> dict[str, int]:
    real, imag = value["re"], value["im"]
    twiddle_real, twiddle_imag = twiddle
    return {
        "re": _saturate(
            _nearest_even(real * twiddle_real - imag * twiddle_imag)
        ),
        "im": _saturate(
            _nearest_even(real * twiddle_imag + imag * twiddle_real)
        ),
    }


def _stage_blocks(
    stream: tuple[dict[str, int], ...], depth: int
) -> tuple[dict[str, int], ...]:
    """Apply one exact radix-2 DIF stage to consecutive ``2 * depth`` blocks."""

    block_size = depth * 2
    assert len(stream) % block_size == 0
    output: list[dict[str, int]] = []
    for offset in range(0, len(stream), block_size):
        block = stream[offset : offset + block_size]
        output.extend(
            _add(block[index], block[index + depth])
            for index in range(depth)
        )
        output.extend(
            _multiply_twiddle(
                _subtract(block[index], block[index + depth]),
                TWIDDLES[depth][index],
            )
            for index in range(depth)
        )
    return tuple(output)


def _oracle(frame: tuple[dict[str, int], ...]) -> tuple[dict[str, int], ...]:
    assert len(frame) == 8
    return _stage_blocks(_stage_blocks(_stage_blocks(frame, 4), 2), 1)


def _fixture() -> tuple[dict[str, int], ...]:
    return tuple(
        {"re": 1000 + index * 137, "im": -300 + index * 83}
        for index in range(8)
    )


def _cycle(
    payload: dict[str, int] | None,
    *,
    ready: int = 1,
) -> dict[str, object]:
    return {
        "input": {
            "payload": dict(payload or ZERO),
            "valid": int(payload is not None),
        },
        "output": {"ready": ready},
    }


def _transfers(results: list[dict[str, object]]) -> tuple[dict[str, int], ...]:
    return tuple(
        item["output"]["payload"]
        for item in results
        if item["output"]["transfer"]
    )


def test_fft8_continuous_stream_matches_staged_oracle_at_ii_one() -> None:
    frame = _fixture()
    expected = (
        {"re": 11836, "im": -76},
        {"re": -548, "im": -332},
        {"re": -880, "im": 216},
        {"re": -216, "im": -880},
        {"re": -1349, "im": 991},
        {"re": -411, "im": -559},
        {"re": -685, "im": -105},
        {"re": 253, "im": -1655},
    )
    assert _oracle(frame) == expected
    # Seven accepted zeros are ordinary samples from the following stream
    # frame.  They advance every result of this finite fixture through the SDF
    # chain; the wrapper has no special flush operation.
    tokens = (*frame, *(dict(ZERO) for _ in range(7)))
    cycles = [_cycle(None), *(_cycle(token) for token in tokens)]
    cycles.extend(_cycle(None) for _ in range(8))

    results = simulate_cycles(
        _fft8(), cycles, reset=[True] + [False] * (len(cycles) - 1)
    )

    assert _transfers(results) == expected
    transfer_cycles = [
        index
        for index, item in enumerate(results)
        if item["output"]["transfer"]
    ]
    assert transfer_cycles == list(range(11, 19))


def test_fft8_gaps_stalls_and_reset_start_a_new_stream_epoch() -> None:
    frame = _fixture()
    # Two pre-reset samples must not affect the post-reset transform.  The
    # three extra zero attempts compensate for the three cycles rejected by
    # propagated downstream backpressure, leaving seven accepted padding
    # samples after the complete frame.
    stream = [
        {"re": 9, "im": 11},
        {"re": 13, "im": 17},
        None,
        frame[0],
        frame[1],
        None,
        frame[2],
        frame[3],
        frame[4],
        None,
        frame[5],
        frame[6],
        frame[7],
        *(dict(ZERO) for _ in range(10)),
        *(None for _ in range(12)),
    ]
    cycles = [
        _cycle(payload, ready=int(index not in (15, 16, 17)))
        for index, payload in enumerate(stream)
    ]
    resets = [False, False, True] + [False] * (len(cycles) - 3)

    results = simulate_cycles(_fft8(), cycles, reset=resets)

    assert _transfers(results) == _oracle(frame)
    stalled = [
        item["output"]["payload"]
        for item in results
        if item["output"]["valid"] and not item["output"]["transfer"]
    ]
    assert stalled == [_oracle(frame)[0]] * 3
    assert [
        index
        for index, item in enumerate(results)
        if item["output"]["transfer"]
    ] == list(range(18, 26))
    assert [
        index
        for index, item in enumerate(results)
        if cycles[index]["input"]["valid"] and not item["input"]["transfer"]
    ] == [15, 16, 17]
