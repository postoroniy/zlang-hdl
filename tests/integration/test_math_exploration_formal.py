"""Real scalar correlator triangle and mutation tutorial acceptance."""

import json
from pathlib import Path
import shutil

import pytest

from tools.math_exploration_formal import run
from zlang.equivalence_result_codec import equivalence_result_from_data
from zlang.toolchain import find_clash_executable
from zlang.verification_bundle import load_verification_bundle


REAL_TOOLS = bool(
    find_clash_executable()
    and all(shutil.which(name) for name in ("sby", "yosys", "yosys-smtbmc", "z3"))
)


@pytest.mark.skipif(not REAL_TOOLS, reason="real Clash/SBY/Yosys/Z3 required")
def test_math_exploration_bounded_triangle_and_real_mutations(tmp_path: Path) -> None:
    output = tmp_path / "formal"
    summary = run(output, depth=10, timeout=120)
    assert summary["accepted"]
    assert summary["candidate_statuses"] == ["bounded_pass"] * 3
    checks = summary["checks"]
    assert checks["shallow_window"]["depth"] == (
        checks["shallow_window"]["minimum_bmc_depth"] - 1
    )
    assert "comparison_window_unreached" in checks["shallow_window"]["reason"]
    for name in ("output_bit_flip", "missing_final_stage"):
        result = equivalence_result_from_data(json.loads((output / name / "result.json").read_text()))
        assert result.status.value == "failed"
        assert result.counterexample is not None
        assert result.counterexample.values
        assert result.counterexample.failure_cycle >= (
            checks["shallow_window"]["minimum_bmc_depth"]
        )
        assert result.reference_hash == checks["shallow_window"]["reference_hash"]
        assert result.implementation_hash != checks["shallow_window"]["implementation_hash"]
        assert tuple((output / name / "work").rglob("*.vcd"))
    # Mutating separate implementation copies never invalidates the original.
    load_verification_bundle(output / "bundle")
