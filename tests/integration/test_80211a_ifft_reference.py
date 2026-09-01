"""Bounded validation for the concise IEEE-authoritative IFFT reference.

The complete N=64 source is a numerical/semantic reference, not the intended
physical architecture.  It must remain compact and bit exact without eagerly
cloning 4096 complex product bodies.  N=8 and N=16 witnesses exercise the same
generic source shape through the physical backends.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from functools import lru_cache
from hashlib import sha256
from collections import Counter
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import (
    ClashPublicTopWrapper,
    bind_artifact_to_public_wrapper,
)
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file
from zlang.ir import expressions as expr
from zlang.ir.functional_regions import evaluate_compile_time
from zlang.ir.types import FixedType, StructType, VecType
from zlang.opt import OptimizationStage, lower, restore
from zlang.simulate import simulate
from zlang.toolchain import generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE64 = ROOT / "docs/reproducers/ifft64_whole_vector_elaboration.zl"
WITNESS_SOURCE = ROOT / "tests/fixtures/fft/ifft_whole_vector_witness.zl"
TOP64 = "IFFT64WholeVectorElaboration"

SAMPLE_MIN = -(1 << 15)
SAMPLE_MAX = (1 << 15) - 1
FINAL_RESCALE = 1 << (37 - 15)
PI = Decimal(
    "3.14159265358979323846264338327950288419716939937510"
    "58209749445923078164062862089986280348253421170679"
)

# Filled from the independent Decimal/integer oracle below.  These digests
# intentionally do not originate from ZLang's compile-time evaluator.
TWIDDLE64_DIGEST = (
    "433517be618079b880ef01f119f1f515b0eebbaef4f4bb26a307ed10e45eaab2"
)
REFERENCE64_DIGEST = (
    "a828cec986262425c55011e2efad80c2bd5eff22e88942dd87a30b978d7af602"
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


@lru_cache(maxsize=None)
def _twiddles(size: int) -> tuple[tuple[int, int], ...]:
    """Return inverse exp(+j*2*pi*k/N)/N in signed Q2.22 raw form."""

    result: list[tuple[int, int]] = []
    with localcontext() as context:
        context.prec = 100
        scale = Decimal(1 << 22) / Decimal(size)
        for phase in range(size):
            angle = Decimal(2) * PI * Decimal(phase) / Decimal(size)
            # Reduce the Taylor argument without changing the exact phase.
            if angle > PI:
                angle -= Decimal(2) * PI
            real = (_decimal_cos(angle) * scale).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
            imag = (_decimal_sin(angle) * scale).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
            result.append((int(real), int(imag)))
    return tuple(result)


def _nearest_even_divide(value: int, denominator: int) -> int:
    sign = -1 if value < 0 else 1
    quotient, remainder = divmod(abs(value), denominator)
    doubled = remainder * 2
    if doubled > denominator or (doubled == denominator and quotient & 1):
        quotient += 1
    return sign * quotient


def _saturate_sample(value: int) -> int:
    return max(SAMPLE_MIN, min(SAMPLE_MAX, value))


def _ifft_reference(
    samples: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    """Evaluate exact widened products/sums and one final quantization."""

    size = len(samples)
    twiddles = _twiddles(size)
    output: list[tuple[int, int]] = []
    for destination in range(size):
        real_accumulator = 0
        imag_accumulator = 0
        for source, (real, imag) in enumerate(samples):
            wr, wi = twiddles[(destination * source) % size]
            # fixed<16,15> * fixed<24,22> produces exact fixed<40,37>
            # products. Complex add/sub widens that to fixed<41,37>.
            real_accumulator += real * wr - imag * wi
            imag_accumulator += real * wi + imag * wr
        # The balanced nominal sum is exact and adds log2(N) guard bits.
        # Only this final boundary changes F=37 to F=15.
        output.append(
            (
                _saturate_sample(
                    _nearest_even_divide(real_accumulator, FINAL_RESCALE)
                ),
                _saturate_sample(
                    _nearest_even_divide(imag_accumulator, FINAL_RESCALE)
                ),
            )
        )
    return tuple(output)


def _fixture(size: int) -> tuple[tuple[int, int], ...]:
    return tuple(
        (
            ((index * 1217 + 97) % 28001) - 14000,
            ((index * 1877 + 313) % 26003) - 13001,
        )
        for index in range(size)
    )


def _oracle_vectors(size: int) -> tuple[tuple[tuple[int, int], ...], ...]:
    random_values = random.Random(0x1FF7_0064)
    mapper_like = [(0, 0) for _ in range(size)]
    for index in range(size):
        if index % 4 == 1:
            mapper_like[index] = (32767, 0)
        elif index % 4 == 2:
            mapper_like[index] = (-23170, 23170)
        elif index % 4 == 3:
            mapper_like[index] = (-10362, 31086)
    saturating = tuple(
        (
            32767 if _twiddles(size)[index][0] >= 0 else -32768,
            -32768 if _twiddles(size)[index][1] >= 0 else 32767,
        )
        for index in range(size)
    )
    return (
        tuple((0, 0) for _ in range(size)),
        tuple((16384, -8192) for _ in range(size)),
        ((32767, -32768),) + tuple((0, 0) for _ in range(size - 1)),
        tuple(
            (32767, -32768) if index == min(7, size - 1) else (0, 0)
            for index in range(size)
        ),
        tuple(
            ((32767, -32768) if index % 2 == 0 else (-32768, 32767))
            for index in range(size)
        ),
        tuple(
            (
                random_values.randrange(SAMPLE_MIN, SAMPLE_MAX + 1),
                random_values.randrange(SAMPLE_MIN, SAMPLE_MAX + 1),
            )
            for _ in range(size)
        ),
        tuple(mapper_like),
        saturating,
        _fixture(size),
    )


def _digest(value: object) -> str:
    payload = json.dumps(value, separators=(",", ":")).encode("ascii")
    return sha256(payload).hexdigest()


def _message(
    samples: tuple[tuple[int, int], ...], marker: int = 1
) -> dict[str, object]:
    return {
        "new_message": marker,
        "data": [{"i": real, "q": imag} for real, imag in samples],
    }


def _walk_dataclasses(value: object):
    """Walk retained typed IR without revisiting shared symbolic templates."""

    seen: set[int] = set()

    def visit(item: object):
        if isinstance(item, (str, bytes, int, float, bool, type(None))):
            return
        identity = id(item)
        if identity in seen:
            return
        seen.add(identity)
        yield item
        if isinstance(item, (tuple, list)):
            for child in item:
                yield from visit(child)
        elif isinstance(item, dict):
            for child in item.values():
                yield from visit(child)
        elif is_dataclass(item):
            for field in fields(item):
                yield from visit(getattr(item, field.name))

    yield from visit(value)


def _complex_fixed_type(type_: object, *, width: int, fraction: int) -> bool:
    return (
        isinstance(type_, StructType)
        and type_.name == f"Complex<fixed<{width},{fraction}>>"
        and len(type_.fields) == 2
        and all(
            field.type == FixedType(width, fraction) for field in type_.fields
        )
    )


def _region_twiddle_row(
    region: expr.FunctionalRegion,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    assert isinstance(region.template, expr.Call)
    assert len(region.template.arguments) == 2
    twiddle = region.template.arguments[1]
    assert isinstance(twiddle, expr.StructConstruct)
    fields_by_name = dict(twiddle.fields)
    tables = {table.name: table for table in region.tables}

    def value_at(value: expr.Expression, index: int) -> int:
        if isinstance(value, expr.Constant):
            return value.value
        assert isinstance(value, expr.FunctionalTableLookup)
        table = tables[value.table_name]
        selected = evaluate_compile_time(
            value.index,
            {region.binder.identity: index},
        )
        selected_value = table.values[selected - table.start]
        assert isinstance(selected_value, expr.Constant)
        return selected_value.value

    return tuple(
        tuple(value_at(fields_by_name[name], index) for index in range(64))
        for name in ("re", "im")
    )


@pytest.fixture(scope="module")
def ifft64_compilation():
    return compile_file(SOURCE64, top=TOP64, include_clash=False)


def test_independent_inverse_twiddle_and_reference_images_are_frozen() -> None:
    twiddles = _twiddles(64)
    assert twiddles[0] == (65536, 0)
    assert twiddles[16] == (0, 65536)
    assert twiddles[32] == (-65536, 0)
    assert twiddles[48] == (0, -65536)
    assert _digest(twiddles) == TWIDDLE64_DIGEST
    assert _digest(_ifft_reference(_fixture(64))) == REFERENCE64_DIGEST
    directed = _oracle_vectors(64)
    assert any(
        component in {SAMPLE_MIN, SAMPLE_MAX}
        for samples in directed
        for pair in _ifft_reference(samples)
        for component in pair
    )

    # Retain at least one exact half-way post-accumulation case so changing
    # nearest-even to a directional rounding policy is observable.
    half = FINAL_RESCALE // 2
    assert any(
        abs(accumulator) % FINAL_RESCALE == half
        for samples in directed
        for destination in range(64)
        for accumulator in (
            sum(
                real * twiddles[(destination * source) % 64][0]
                - imag * twiddles[(destination * source) % 64][1]
                for source, (real, imag) in enumerate(samples)
            ),
            sum(
                real * twiddles[(destination * source) % 64][1]
                + imag * twiddles[(destination * source) % 64][0]
                for source, (real, imag) in enumerate(samples)
            ),
        )
    )


def test_ifft64_symbolic_types_quantization_and_canonical_round_trip(
    ifft64_compilation,
) -> None:
    module = ifft64_compilation.ir
    objects = tuple(_walk_dataclasses(module))
    reductions = [item for item in objects if isinstance(item, expr.Reduce)]
    exact = [
        reduction
        for reduction in reductions
        if _complex_fixed_type(reduction.type, width=47, fraction=37)
    ]
    # The first bounded implementation may retain one definition per exact
    # OUT value specialization, or one binder-parametric template. Both are
    # legal; cloning a body independently at every *use* is not.
    assert len(exact) == 64
    assert all(isinstance(item.collection.type, VecType) for item in exact)
    assert all(item.collection.type.length == 64 for item in exact)
    assert all(
        _complex_fixed_type(
            item.collection.type.element_type, width=41, fraction=37
        )
        for item in exact
    )
    assert all(isinstance(item.collection, expr.FunctionalRegion) for item in exact)
    assert all(item.expanded is None and item.plan is not None for item in exact)
    assert all(len(item.plan.levels) == 6 for item in exact if item.plan is not None)
    assert all(
        tuple(
            level.output_types[0].fields[0].type.width
            for level in item.plan.levels
        )
        == (42, 43, 44, 45, 46, 47)
        for item in exact
        if item.plan is not None
    )

    product_regions = [
        item
        for item in objects
        if isinstance(item, expr.FunctionalRegion)
        and _complex_fixed_type(item.type.element_type, width=41, fraction=37)
    ]
    assert len(product_regions) == 64
    actual_twiddle_rows = Counter(
        _region_twiddle_row(region) for region in product_regions
    )
    expected_twiddle_rows = Counter(
        (
            tuple(_twiddles(64)[(output * index) % 64][0] for index in range(64)),
            tuple(_twiddles(64)[(output * index) % 64][1] for index in range(64)),
        )
        for output in range(64)
    )
    assert actual_twiddle_rows == expected_twiddle_rows

    final_conversions = [
        item
        for item in objects
        if isinstance(item, expr.FixedConvert)
        and item.kind is expr.FixedConversionKind.RESCALE
        and item.type == FixedType(16, 15)
    ]
    # The exact Complex quantizer specialization is shared by every bin: one
    # final conversion per component, not one cloned pair per use.
    assert len(final_conversions) == 2
    assert all(item.expression.type == FixedType(47, 37) for item in final_conversions)
    assert all(
        item.rounding is expr.FixedRounding.NEAREST_EVEN
        and item.overflow is expr.FixedOverflow.SATURATE
        for item in final_conversions
    )
    assert [
        item
        for item in objects
        if isinstance(item, expr.FixedConvert)
        and item.kind is expr.FixedConversionKind.RESCALE
    ] == final_conversions

    definitions = tuple(module.callable_definitions)
    definition_ids = tuple(item.callee_identity for item in definitions)
    assert len(definition_ids) == len(set(definition_ids))
    assert definition_ids == tuple(sorted(definition_ids))
    calls = [item for item in objects if isinstance(item, expr.Call)]
    retained_calls = [item for item in calls if item.callee_identity is not None]
    assert retained_calls
    assert {item.callee_identity for item in retained_calls} <= set(definition_ids)
    # At least one exact specialization is reused. This is the inspectable
    # guard against reverting to per-use generic/operator body cloning.
    usage = Counter(item.callee_identity for item in retained_calls)
    assert max(usage.values()) > 1

    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    assert len(module.callable_definitions) <= 80
    assert len(canonical.expressions) <= 1_000
    assert restore(canonical) == module


def test_ifft64_semantic_simulator_matches_independent_integer_oracle(
    ifft64_compilation,
) -> None:
    module = ifft64_compilation.ir
    for ordinal, samples in enumerate(_oracle_vectors(64)):
        marker = ordinal & 1
        expected = _ifft_reference(samples)
        actual = simulate(module, input=_message(samples, marker))["output"]
        assert actual["new_message"] == marker
        assert actual["data"] == [
            {"i": real, "q": imag} for real, imag in expected
        ]


@pytest.mark.performance
def test_ifft64_semantic_and_canonical_subprocess_has_a_bounded_regression_ceiling() -> None:
    script = r'''
import json, resource, sys
from time import perf_counter
from zlang.compiler import compile_file
from zlang.opt import OptimizationStage, lower, restore
started = perf_counter()
result = compile_file(sys.argv[1], top="IFFT64WholeVectorElaboration", include_clash=False)
semantic_elapsed = perf_counter() - started
semantic_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
canonical = lower(result.ir, stage=OptimizationStage.HIGH_LEVEL)
assert restore(canonical) == result.ir
first_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
second = compile_file(sys.argv[1], top="IFFT64WholeVectorElaboration", include_clash=False)
second_canonical = lower(second.ir, stage=OptimizationStage.HIGH_LEVEL)
assert restore(second_canonical) == second.ir
assert tuple(item.callee_identity for item in result.ir.callable_definitions) == tuple(
    item.callee_identity for item in second.ir.callable_definitions
)
assert len(canonical.expressions) == len(second_canonical.expressions)
print(json.dumps({
    "semantic_elapsed": semantic_elapsed,
    "semantic_rss_kib": semantic_rss_kib,
    "combined_elapsed": perf_counter() - started,
    "combined_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    "repeat_rss_growth_kib": (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - first_rss_kib
    ),
}, sort_keys=True))
'''
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        (sys.executable, "-c", script, str(SOURCE64)),
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    measurement = json.loads(completed.stdout.splitlines()[-1])
    # These are hard acceptance gates, not benchmark aspirations.  Checking
    # semantic construction separately prevents canonical work from hiding a
    # recurrence of eager semantic expansion.
    assert measurement["semantic_elapsed"] <= 30
    assert measurement["semantic_rss_kib"] <= 512 * 1024
    assert measurement["combined_elapsed"] <= 45
    assert measurement["combined_rss_kib"] <= 768 * 1024
    assert measurement["repeat_rss_growth_kib"] <= 128 * 1024


def _sv_lane_values(samples: tuple[tuple[int, int], ...], lane: int) -> str:
    return "'{" + ", ".join(
        f"16'h{sample[lane] & 0xFFFF:04x}" for sample in samples
    ) + "}"


def _sv_output_checks(
    samples: tuple[tuple[int, int], ...], marker: int, label: str
) -> str:
    checks = [
        f"if (output_new_message !== 1'b{marker}) "
        f'$fatal(1, "{label} marker mismatch");'
    ]
    for index, (real, imag) in enumerate(samples):
        checks.append(
            f"if (output_data_i[{index}] !== 16'h{real & 0xFFFF:04x}) "
            f'$fatal(1, "{label} real lane {index}");'
        )
        checks.append(
            f"if (output_data_q[{index}] !== 16'h{imag & 0xFFFF:04x}) "
            f'$fatal(1, "{label} imag lane {index}");'
        )
    return "\n    ".join(checks)


def _bench(size: int) -> str:
    first = _fixture(size)
    second = tuple(reversed(first))
    expected0 = _ifft_reference(first)
    expected1 = _ifft_reference(second)
    return f"""
module tb;
  logic input_new_message;
  logic signed [15:0] input_data_i [0:{size - 1}];
  logic signed [15:0] input_data_q [0:{size - 1}];
  wire output_new_message;
  wire signed [15:0] output_data_i [0:{size - 1}];
  wire signed [15:0] output_data_q [0:{size - 1}];
  IFFT{size}WholeVectorWitness dut(
    .input_new_message, .input_data_i, .input_data_q,
    .output_new_message, .output_data_i, .output_data_q
  );
  initial begin
    input_new_message = 1'b0;
    input_data_i = {_sv_lane_values(first, 0)};
    input_data_q = {_sv_lane_values(first, 1)};
    #1;
    {_sv_output_checks(expected0, 0, f"IFFT{size} marker-zero")}
    input_new_message = 1'b1;
    input_data_i = {_sv_lane_values(second, 0)};
    input_data_q = {_sv_lane_values(second, 1)};
    #1;
    {_sv_output_checks(expected1, 1, f"IFFT{size} marker-one")}
    $finish;
  end
endmodule
"""


@pytest.mark.parametrize("size", (8, 16))
def test_bounded_ifft_witness_semantic_simulator_is_bit_exact(size: int) -> None:
    top = f"IFFT{size}WholeVectorWitness"
    module = compile_file(WITNESS_SOURCE, top=top, include_clash=False).ir
    for marker, samples in ((0, _fixture(size)), (1, tuple(reversed(_fixture(size))))):
        actual = simulate(module, input=_message(samples, marker))["output"]
        assert actual["new_message"] == marker
        assert actual["data"] == [
            {"i": real, "q": imag}
            for real, imag in _ifft_reference(samples)
        ]


def _verilate_and_run(
    tmp_path: Path, rtl: tuple[Path, ...], bench: str, suffix: str
) -> None:
    testbench = tmp_path / f"{suffix}_tb.sv"
    object_dir = tmp_path / f"obj_{suffix}"
    testbench.write_text(bench)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "--Mdir", str(object_dir), "-Wno-DECLFILENAME", "-Wno-UNUSED",
            "-Wno-UNDRIVEN", *(str(path) for path in rtl), str(testbench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    executed = subprocess.run(
        (str(object_dir / "Vtb"),), capture_output=True, text=True
    )
    assert executed.returncode == 0, executed.stderr or executed.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize("size", (8, 16))
def test_direct_sv_bounded_ifft_witness_is_bit_exact(
    tmp_path: Path, size: int
) -> None:
    top = f"IFFT{size}WholeVectorWitness"
    module = compile_file(WITNESS_SOURCE, top=top, include_clash=False).ir
    artifact = emit_sv_artifact(module)
    repeated = emit_sv_artifact(module)
    assert artifact.text == repeated.text
    assert artifact.artifact_hash == repeated.artifact_hash
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.bindings == artifact.bindings
    bindings = {
        item.semantic_signal_id: item.rtl_path for item in artifact.bindings
    }
    assert bindings["port:input"] == ""
    assert bindings["port:output"] == ""
    assert bindings["port:input.new_message"] == "input_new_message"
    assert bindings["port:input.data.i"] == "input_data_i"
    assert bindings["port:input.data.q"] == "input_data_q"
    assert bindings["port:output.new_message"] == "output_new_message"
    assert bindings["port:output.data.i"] == "output_data_i"
    assert bindings["port:output.data.q"] == "output_data_q"
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), top)
    _verilate_and_run(tmp_path, (rtl,), _bench(size), f"sv_{size}")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize("size", (8, 16))
def test_real_clash_bounded_ifft_witness_is_bit_exact(
    tmp_path: Path, size: int
) -> None:
    top = f"IFFT{size}WholeVectorWitness"
    compilation = compile_file(WITNESS_SOURCE, top=top)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)
    artifact = bind_artifact_to_public_wrapper(
        emit_clash_artifact(compilation.ir), wrapper
    )
    repeated = bind_artifact_to_public_wrapper(
        emit_clash_artifact(compilation.ir), wrapper
    )
    assert artifact.text == repeated.text == compilation.clash
    assert artifact.artifact_hash == repeated.artifact_hash
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.bindings == artifact.bindings
    bindings = {
        item.semantic_signal_id: item.rtl_path for item in artifact.bindings
    }
    assert bindings["port:input"] == ""
    assert bindings["port:output"] == ""
    assert bindings["port:input.new_message"] == "input_new_message"
    assert bindings["port:input.data.i"] == "input_data_i"
    assert bindings["port:input.data.q"] == "input_data_q"
    assert bindings["port:output.new_message"] == "output_new_message"
    assert bindings["port:output.data.i"] == "output_data_i"
    assert bindings["port:output.data.q"] == "output_data_q"
    rtl = generate_verilog(
        artifact.text,
        top,
        tmp_path / f"clash_{size}_rtl",
        CLASH_EXECUTABLE,
        companions=artifact.companions,
        public_wrapper=wrapper,
    )
    lint_with_verilator(rtl, top)
    _verilate_and_run(
        tmp_path, tuple(rtl), _bench(size), f"clash_{size}"
    )
