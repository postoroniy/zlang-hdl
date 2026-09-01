"""Bounded simulator validation for the four-stage FFT16 SDF reference.

The oracle is independent of the ZLang evaluator and simulator.  It performs
integer fixed-point arithmetic and applies nearest-even/saturating conversion
once at every D=8, D=4, D=2, and D=1 architectural stage boundary.
"""

from functools import lru_cache
from pathlib import Path

from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zl"
SAMPLE_WIDTH = 18
SAMPLE_MIN = -(1 << (SAMPLE_WIDTH - 1))
SAMPLE_MAX = (1 << (SAMPLE_WIDTH - 1)) - 1
TWIDDLE_SCALE = 1 << 14
ZERO = {"re": 0, "im": 0}

TWIDDLES = {
    8: (
        (16384, 0),
        (15137, -6270),
        (11585, -11585),
        (6270, -15137),
        (0, -16384),
        (-6270, -15137),
        (-11585, -11585),
        (-15137, -6270),
    ),
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
def _fft16():
    return compile_source(
        SOURCE.read_text(), top="FFT16SDFReference", include_clash=False
    ).ir


def _saturate(value: int) -> int:
    return min(SAMPLE_MAX, max(SAMPLE_MIN, value))


def _nearest_even(numerator: int, denominator: int = TWIDDLE_SCALE) -> int:
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
    """Apply one radix-2 DIF stage to consecutive ``2 * depth`` blocks."""

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
    assert len(frame) == 16
    stream = frame
    for depth in (8, 4, 2, 1):
        stream = _stage_blocks(stream, depth)
    return stream


def _fixture() -> tuple[dict[str, int], ...]:
    return tuple(
        {"re": 700 + index * 61, "im": -500 + index * 47}
        for index in range(16)
    )


def _expected() -> tuple[dict[str, int], ...]:
    # Stream order is the four-bit bit reversal:
    # 0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15.
    return (
        {"re": 18520, "im": -2360},
        {"re": -488, "im": -376},
        {"re": -864, "im": 112},
        {"re": -112, "im": -864},
        {"re": -1396, "im": 802},
        {"re": -332, "im": -578},
        {"re": -644, "im": -174},
        {"re": 420, "im": -1554},
        {"re": -2379, "im": 2077},
        {"re": -413, "im": -473},
        {"re": -739, "im": -49},
        {"re": 75, "im": -1107},
        {"re": -1051, "im": 353},
        {"re": -237, "im": -701},
        {"re": -563, "im": -279},
        {"re": 1403, "im": -2829},
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


def test_fft16_continuous_stream_matches_staged_oracle_at_ii_one() -> None:
    frame = _fixture()
    expected = _expected()
    assert _oracle(frame) == expected

    # Fifteen accepted zeros are normal samples from the following frame. They
    # advance every result of the finite fixture through the continuous SDF
    # chain; there is no special flush operation.
    tokens = (*frame, *(dict(ZERO) for _ in range(15)))
    cycles = [_cycle(None), *(_cycle(token) for token in tokens)]
    cycles.extend(_cycle(None) for _ in range(10))

    results = simulate_cycles(
        _fft16(), cycles, reset=[True] + [False] * (len(cycles) - 1)
    )

    assert _transfers(results) == expected
    assert [
        index
        for index, item in enumerate(results)
        if item["output"]["transfer"]
    ] == list(range(20, 36))


def test_fft16_gaps_stalls_and_reset_start_a_new_stream_epoch() -> None:
    frame = _fixture()
    expected = _expected()
    # Three pre-reset samples are discarded. Two source gaps exercise accepted-
    # transfer phase counting. Four extra zero attempts compensate for the
    # four cycles rejected by propagated output backpressure, leaving fifteen
    # accepted post-frame padding samples.
    post_reset_stream = [
        *frame[:3],
        None,
        *frame[3:10],
        None,
        *frame[10:],
    ]
    stream = [
        {"re": 9, "im": 11},
        {"re": 13, "im": 17},
        {"re": 19, "im": 23},
        None,
        *post_reset_stream,
        *(dict(ZERO) for _ in range(19)),
        *(None for _ in range(16)),
    ]
    stalled_cycles = (25, 26, 27, 28)
    cycles = [
        _cycle(payload, ready=int(index not in stalled_cycles))
        for index, payload in enumerate(stream)
    ]
    resets = [False, False, False, True] + [False] * (len(cycles) - 4)

    results = simulate_cycles(_fft16(), cycles, reset=resets)

    assert _transfers(results) == expected
    stalled = [
        item["output"]["payload"]
        for item in results
        if item["output"]["valid"] and not item["output"]["transfer"]
    ]
    assert stalled == [expected[0]] * len(stalled_cycles)
    assert [
        index
        for index, item in enumerate(results)
        if item["output"]["transfer"]
    ] == list(range(29, 45))
    assert [
        index
        for index, item in enumerate(results)
        if cycles[index]["input"]["valid"] and not item["input"]["transfer"]
    ] == list(stalled_cycles)
