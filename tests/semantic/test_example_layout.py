from pathlib import Path

from zlang.compiler import compile_source
from zlang.opt import OptimizationStage, lower, restore


ROOT = Path(__file__).resolve().parents[2]


def test_retired_unsupported_directory_is_absent_and_nested_fft_examples_are_discovered():
    examples = ROOT / "examples"
    assert not (examples / "unsupported").exists()
    discovered = {path.relative_to(ROOT) for path in examples.rglob("*.zl")}
    assert Path("examples/fft/complex_multiply_pipeline_auto.zl") in discovered
    assert Path("examples/fft/sdf_stage_numeric.zl") in discovered


def test_promoted_complex_fft_tops_round_trip_semantic_ir():
    source = (ROOT / "examples/fft/complex_multiply_pipeline_auto.zl").read_text()
    for top in ("FFTComplexMultiplyRealAuto", "FFTComplexMultiplyImagAuto"):
        result = compile_source(source, top=top)
        restored = restore(lower(result.ir, stage=OptimizationStage.HIGH_LEVEL))
        assert restored == result.ir
