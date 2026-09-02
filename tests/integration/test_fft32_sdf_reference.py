"""Bounded simulator validation for the five-stage FFT32 SDF reference.

The numerical oracle is independent of the ZLang evaluator and simulator. It
uses hard-coded Q2.14 twiddles and applies nearest-even/saturating conversion at
each D=16, D=8, D=4, D=2, and D=1 architectural stage boundary.
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

# Exact nearest-even Q2.14 encodings of exp(-j * 2*pi*k/(2*depth)).  The
# depth-16 table is checked below against the independently elaborated ROM.
TWIDDLES = {
    16: (
        (16384, 0),
        (16069, -3196),
        (15137, -6270),
        (13623, -9102),
        (11585, -11585),
        (9102, -13623),
        (6270, -15137),
        (3196, -16069),
        (0, -16384),
        (-3196, -16069),
        (-6270, -15137),
        (-9102, -13623),
        (-11585, -11585),
        (-13623, -9102),
        (-15137, -6270),
        (-16069, -3196),
    ),
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
def _fft32():
    return compile_source(
        SOURCE.read_text(), top="FFT32SDFReference", include_clash=False
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
    assert len(frame) == 32
    stream = frame
    for depth in (16, 8, 4, 2, 1):
        stream = _stage_blocks(stream, depth)
    return stream


def _fixture() -> tuple[dict[str, int], ...]:
    return tuple(
        {"re": 300 + index * 29, "im": -700 + index * 31}
        for index in range(32)
    )


def _expected() -> tuple[dict[str, int], ...]:
    # Five-bit bit-reversed stream order: 0,16,8,24,...,15,31.
    return (
        {"re": 23984, "im": -7024},
        {"re": -464, "im": -496},
        {"re": -960, "im": -32},
        {"re": 32, "im": -960},
        {"re": -1662, "im": 624},
        {"re": -258, "im": -688},
        {"re": -670, "im": -304},
        {"re": 734, "im": -1616},
        {"re": -2958, "im": 1836},
        {"re": -366, "im": -588},
        {"re": -796, "im": -186},
        {"re": 280, "im": -1190},
        {"re": -1206, "im": 198},
        {"re": -134, "im": -806},
        {"re": -562, "im": -404},
        {"re": 2030, "im": -2828},
        {"re": -5501, "im": 4213},
        {"re": -415, "im": -541},
        {"re": -871, "im": -115},
        {"re": 139, "im": -1061},
        {"re": -1393, "im": 372},
        {"re": -199, "im": -744},
        {"re": -612, "im": -355},
        {"re": 1172, "im": -2025},
        {"re": -2100, "im": 1032},
        {"re": -312, "im": -636},
        {"re": -730, "im": -248},
        {"re": 462, "im": -1364},
        {"re": -1069, "im": 70},
        {"re": -55, "im": -878},
        {"re": -512, "im": -451},
        {"re": 4572, "im": -5205},
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


def test_fft32_continuous_stream_matches_staged_oracle_at_ii_one() -> None:
    module = _fft32()
    depth_16_rom = next(
        child.roms[0]
        for child in module.children
        if child.roms[0].depth == 16
    )
    semantic_twiddles = tuple(
        (word.fields[0][1].value, word.fields[1][1].value)
        for word in depth_16_rom.contents
    )
    assert semantic_twiddles == TWIDDLES[16]

    frame = _fixture()
    expected = _expected()
    assert _oracle(frame) == expected
    tokens = (*frame, *(dict(ZERO) for _ in range(31)))
    cycles = [_cycle(None), *(_cycle(token) for token in tokens)]
    cycles.extend(_cycle(None) for _ in range(8))

    results = simulate_cycles(
        module, cycles, reset=[True] + [False] * (len(cycles) - 1)
    )

    assert _transfers(results) == expected
    assert [
        index
        for index, item in enumerate(results)
        if item["output"]["transfer"]
    ] == list(range(37, 69))


def test_fft32_gaps_stalls_and_reset_start_a_new_stream_epoch() -> None:
    frame = _fixture()
    expected = _expected()
    post_reset_stream = [
        *frame[:5],
        None,
        *frame[5:18],
        None,
        *frame[18:26],
        None,
        *frame[26:],
    ]
    # Five additional zero attempts compensate exactly for the five cycles
    # rejected by propagated backpressure, leaving 31 accepted padding samples.
    stream = [
        {"re": 1, "im": 2},
        {"re": 3, "im": 4},
        {"re": 5, "im": 6},
        {"re": 7, "im": 8},
        None,
        *post_reset_stream,
        *(dict(ZERO) for _ in range(36)),
        *(None for _ in range(8)),
    ]
    stalled_cycles = tuple(range(44, 49))
    cycles = [
        _cycle(payload, ready=int(index not in stalled_cycles))
        for index, payload in enumerate(stream)
    ]
    resets = [False, False, False, False, True] + [False] * (len(cycles) - 5)

    results = simulate_cycles(_fft32(), cycles, reset=resets)

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
    ] == list(range(49, 81))
    assert [
        index
        for index, item in enumerate(results)
        if cycles[index]["input"]["valid"] and not item["input"]["transfer"]
    ] == list(stalled_cycles)
