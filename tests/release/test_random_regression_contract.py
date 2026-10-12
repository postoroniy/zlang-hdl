"""Static contracts for reproducible random-source generation."""

from __future__ import annotations

from tools.random_regression import derived_seed, generate, parse_simulator_values


def test_case_seed_is_stable_and_distinct() -> None:
    assert derived_seed("20260923", "expressions", 7) == derived_seed(
        "20260923", "expressions", 7
    )
    assert derived_seed("20260923", "expressions", 7) != derived_seed(
        "20260923", "expressions", 8
    )
    assert derived_seed("20260923", "expressions", 7) != derived_seed(
        "20260923", "diagnostics", 7
    )


def test_generated_case_is_exactly_reproducible() -> None:
    seed = derived_seed("20260923", "expressions", 3)
    assert generate("expressions", seed) == generate("expressions", seed)
    source, expectation, oracle = generate("expressions", seed)
    assert source.endswith("}\n")
    assert expectation == "valid"
    assert oracle is not None
    assert set(oracle) == {"0", "1", "127", "255"}


def test_invalid_cases_have_no_behavioral_oracle() -> None:
    source, expectation, oracle = generate("diagnostics", 9)
    assert source.startswith("module Random")
    assert expectation == "invalid"
    assert oracle is None


def test_vvp_finish_banner_is_not_a_numeric_sample() -> None:
    output = "0:232\n1:233\n/tmp/case/tb.sv:10: $finish called at 4 (1s)\n"
    assert parse_simulator_values(output) == {"0": "232", "1": "233"}
