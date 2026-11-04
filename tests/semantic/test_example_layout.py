from pathlib import Path
import re

import pytest

from zlang.compiler import compile_source
from zlang.native_simulation import simulate_cycles
from zlang.opt import OptimizationStage, lower, restore


ROOT = Path(__file__).resolve().parents[2]


def test_retired_unsupported_directory_is_absent_and_nested_fft_examples_are_discovered():
    examples = ROOT / "examples"
    assert not (examples / "unsupported").exists()
    discovered = {path.relative_to(ROOT) for path in examples.rglob("*.zhl")}
    assert Path("examples/fft/complex_multiply_pipeline_auto.zhl") in discovered
    assert Path("examples/fft/sdf_stage_numeric.zhl") in discovered


def test_promoted_complex_fft_tops_round_trip_semantic_ir():
    source = (ROOT / "examples/fft/complex_multiply_pipeline_auto.zhl").read_text()
    for top in ("FFTComplexMultiplyRealAuto", "FFTComplexMultiplyImagAuto"):
        result = compile_source(source, top=top)
        restored = restore(lower(result.ir, stage=OptimizationStage.HIGH_LEVEL))
        assert restored == result.ir


@pytest.mark.parametrize(("document", "top", "width"), (
    ("language-overview.md", "RunningSum", 10),
    ("language-quick-reference.md", "Accumulator", 9),
))
def test_documented_accumulators_keep_exact_addition_and_explicit_wrap(document, top, width):
    text = (ROOT / "docs" / document).read_text()
    match = re.search(rf"```zlang\n(module {top} \{{.*?\n\}})\n```", text, re.S)
    assert match is not None
    module = compile_source(match.group(1), top=top).ir
    cycles = []
    expected = []
    resets = []
    total = 0
    for index in range(128):
        enable = int(index % 7 != 0)
        a, b = (index * 97) & 255, (index * 61) & 255
        asserted = index in (0, 17, 96)
        resets.append(asserted)
        if asserted:
            total = 0
        expected.append(total)
        cycles.append({"enable": enable, "a": a, "b": b}
                      if top == "RunningSum" else {"enable": enable, "value": a})
        if enable and not asserted:
            total = (total + a + (b if top == "RunningSum" else 0)) % (1 << width)
    samples = simulate_cycles(module, cycles, reset=resets)
    assert [sample["total"] for sample in samples] == expected
