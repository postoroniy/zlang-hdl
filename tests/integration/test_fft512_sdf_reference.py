"""Independent numerical and bounded replay checks for the FFT512 SDF reference.

Numerical expectations originate from independent Decimal twiddles and an
integer implementation of the frozen stage boundaries.  The complete
1,033-cycle hierarchical replay runs in an isolated subprocess with a hard
timeout so scalability regressions fail explicitly.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zhl"
TOP = "FFT512SDFReference"

SAMPLE_MIN = -(1 << 17)
SAMPLE_MAX = (1 << 17) - 1
TWIDDLE_SCALE = 1 << 14

PI = Decimal(
    "3.14159265358979323846264338327950288419716939937510"
    "58209749445923078164062862089986280348253421170679"
)

TWIDDLE_DIGEST = (
    "e5d531425e935a1a30baedfc0aecb476236686320cb0cb763"
    "309ae2ef16bb2bb"
)
OUTPUT_DIGEST = (
    "deb8344501a1976845dc3ac201ceaad1fced353d24187af9f0"
    "08207cc9b73113"
)


def _decimal_sin(value: Decimal) -> Decimal:
    term = value
    total = value
    index = 1
    while True:
        term *= -(value * value) / Decimal((2 * index) * (2 * index + 1))
        updated = total + term
        if updated == total:
            return total
        total = updated
        index += 1


def _decimal_cos(value: Decimal) -> Decimal:
    term = Decimal(1)
    total = term
    index = 1
    while True:
        term *= -(value * value) / Decimal((2 * index - 1) * (2 * index))
        updated = total + term
        if updated == total:
            return total
        total = updated
        index += 1


@lru_cache(maxsize=1)
def _depth_256_twiddles() -> tuple[tuple[int, int], ...]:
    """Generate exp(-j*2*pi*k/512) independently at 100-digit precision."""

    result: list[tuple[int, int]] = []
    with localcontext() as context:
        context.prec = 100
        for index in range(256):
            angle = -Decimal(2) * PI * Decimal(index) / Decimal(512)
            real = (_decimal_cos(angle) * TWIDDLE_SCALE).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
            imag = (_decimal_sin(angle) * TWIDDLE_SCALE).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
            result.append((int(real), int(imag)))
    return tuple(result)


def _canonical_digest(values: tuple[tuple[int, int], ...]) -> str:
    payload = json.dumps(values, separators=(",", ":")).encode("ascii")
    return sha256(payload).hexdigest()


def _fixture() -> tuple[tuple[int, int], ...]:
    return tuple(
        (
            ((index * 211 + 37) % 2000) - 1000,
            ((index * 157 + 91) % 1800) - 900,
        )
        for index in range(512)
    )


def _saturate(value: int) -> int:
    return max(SAMPLE_MIN, min(SAMPLE_MAX, value))


def _nearest_even_divide(value: int, denominator: int = TWIDDLE_SCALE) -> int:
    sign = -1 if value < 0 else 1
    quotient, remainder = divmod(abs(value), denominator)
    if 2 * remainder > denominator or (
        2 * remainder == denominator and quotient & 1
    ):
        quotient += 1
    return sign * quotient


def _add(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    return (
        _saturate(left[0] + right[0]),
        _saturate(left[1] + right[1]),
    )


def _subtract(
    left: tuple[int, int], right: tuple[int, int]
) -> tuple[int, int]:
    return (
        _saturate(left[0] - right[0]),
        _saturate(left[1] - right[1]),
    )


def _multiply_twiddle(
    value: tuple[int, int], twiddle: tuple[int, int]
) -> tuple[int, int]:
    real = value[0] * twiddle[0] - value[1] * twiddle[1]
    imag = value[0] * twiddle[1] + value[1] * twiddle[0]
    return (
        _saturate(_nearest_even_divide(real)),
        _saturate(_nearest_even_divide(imag)),
    )


@lru_cache(maxsize=1)
def _staged_oracle() -> tuple[tuple[int, int], ...]:
    """Apply the frozen quantization boundary at every DIF butterfly stage."""

    values = list(_fixture())
    depth_256 = _depth_256_twiddles()
    for span in (512, 256, 128, 64, 32, 16, 8, 4, 2):
        half = span // 2
        twiddle_stride = 512 // span
        next_values = list(values)
        for base in range(0, 512, span):
            source = values[base : base + span]
            for index in range(half):
                upper = source[index]
                lower = source[index + half]
                difference = _subtract(upper, lower)
                next_values[base + index] = _add(upper, lower)
                next_values[base + half + index] = _multiply_twiddle(
                    difference, depth_256[index * twiddle_stride]
                )
        values = next_values
    return tuple(values)


def _reverse_nine_bits(value: int) -> int:
    return int(f"{value:09b}"[::-1], 2)


def test_depth_256_twiddles_have_frozen_independent_image() -> None:
    twiddles = _depth_256_twiddles()
    assert len(twiddles) == 256
    assert _canonical_digest(twiddles) == TWIDDLE_DIGEST
    assert twiddles[:16] == (
        (16384, 0), (16383, -201), (16379, -402), (16373, -603),
        (16364, -804), (16353, -1005), (16340, -1205), (16324, -1406),
        (16305, -1606), (16284, -1806), (16261, -2006),
        (16235, -2205), (16207, -2404), (16176, -2603),
        (16143, -2801), (16107, -2999),
    )
    assert twiddles[-16:] == (
        (-16069, -3196), (-16107, -2999), (-16143, -2801),
        (-16176, -2603), (-16207, -2404), (-16235, -2205),
        (-16261, -2006), (-16284, -1806), (-16305, -1606),
        (-16324, -1406), (-16340, -1205), (-16353, -1005),
        (-16364, -804), (-16373, -603), (-16379, -402),
        (-16383, -201),
    )


def test_fft512_staged_integer_oracle_has_frozen_digest_and_selected_bins() -> None:
    fixture = _fixture()
    assert fixture[:8] == (
        (-963, -809), (-752, -652), (-541, -495), (-330, -338),
        (-119, -181), (92, -24), (303, 133), (514, 290),
    )
    assert fixture[-8:] == (
        (-619, -881), (-408, -724), (-197, -567), (14, -410),
        (225, -253), (436, -96), (647, 61), (858, 218),
    )

    outputs = _staged_oracle()
    assert len(outputs) == 512
    assert _canonical_digest(outputs) == OUTPUT_DIGEST
    assert {
        position: (_reverse_nine_bits(position), outputs[position])
        for position in (0, 1, 2, 3, 7, 15, 31, 63, 127, 255, 256, 383, 511)
    } == {
        0: (0, (1120, -3696)),
        1: (256, (1984, 3008)),
        2: (128, (-2408, -2376)),
        3: (384, (2376, -2408)),
        7: (448, (3272, -5118)),
        15: (480, (-2874, -3156)),
        31: (496, (-1631, -2525)),
        63: (504, (-595, -2456)),
        127: (508, (735, -5939)),
        255: (510, (-422, 4028)),
        256: (1, (-1186, -3789)),
        383: (509, (509, -13653)),
        511: (511, (711, -1067)),
    }


def test_fft512_semantic_depth_256_rom_matches_independent_table() -> None:
    module = compile_source(
        SOURCE.read_text(), top=TOP, include_clash=False
    ).ir
    assert [child.roms[0].depth for child in module.children] == [
        256, 128, 64, 32, 16, 8, 4, 2, 1
    ]
    depth_256 = next(
        child.roms[0] for child in module.children if child.roms[0].depth == 256
    )
    semantic_words = tuple(
        (word.fields[0][1].value, word.fields[1][1].value)
        for word in depth_256.contents
    )
    assert semantic_words == _depth_256_twiddles()


def test_fft512_bounded_hierarchical_replay() -> None:
    script = r'''from hashlib import sha256
import json
from pathlib import Path

from tests.integration.test_fft512_sdf_reference import _fixture
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles

source = Path("examples/fft/sdf_stage_numeric.zhl")
module = compile_source(
    source.read_text(), top="FFT512SDFReference", include_clash=False
).ir
tokens = [
    {"re": real, "im": imag} for real, imag in _fixture()
] + [{"re": 0, "im": 0} for _ in range(511)]
cycles = [
    {
        "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
        "output": {"ready": 1},
    }
] + [
    {
        "input": {"payload": payload, "valid": 1},
        "output": {"ready": 1},
    }
    for payload in tokens
] + [
    {
        "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
        "output": {"ready": 1},
    }
    for _ in range(9)
]
records = simulate_cycles(
    module, cycles, reset=[True] + [False] * (len(cycles) - 1)
)
outputs = [
    [item["output"]["payload"]["re"], item["output"]["payload"]["im"]]
    for item in records
    if item["output"]["transfer"]
]
input_count = sum(item["input"]["transfer"] for item in records)
transfers = [
    index - 1
    for index, item in enumerate(records)
    if item["output"]["transfer"]
]
print(json.dumps({
    "input_count": input_count,
    "count": len(outputs),
    "digest": sha256(
        json.dumps(outputs, separators=(",", ":")).encode("ascii")
    ).hexdigest(),
    "first": transfers[0] if transfers else None,
    "last": transfers[-1] if transfers else None,
}, sort_keys=True))
'''
    try:
        completed = subprocess.run(
            (sys.executable, "-c", script),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "FFT512 hierarchical replay exceeded the explicit 60-second "
            "scalability bound"
        )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {
        "input_count": 1023,
        "count": 512,
        "digest": OUTPUT_DIGEST,
        "first": 520,
        "last": 1031,
    }
