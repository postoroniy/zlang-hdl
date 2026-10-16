# SPDX-License-Identifier: Apache-2.0
"""Finite-bitvector proof preparation must never assume a widening identity."""

import time

import pytest
import z3

from zlang.formal_bitvector_rewrite import (
    ArithmeticProofUnavailable,
    CheckedBitvectorRewriter,
)
from zlang.formal_rtl_arithmetic import _bounded_degree
from zlang.formal_joint_value import rtl_arithmetic_policy


def rewriter(**kwargs):
    return CheckedBitvectorRewriter(deadline=time.monotonic() + 10, **kwargs)


def test_exact_widening_is_solver_checked_and_replayable():
    a, b = z3.BitVecs("a b", 16)
    original = z3.SignExt(16, z3.SignExt(1, a) - z3.SignExt(1, b))
    owner = rewriter()
    normalized = owner.normalize(original)
    assert z3.is_true(z3.simplify(normalized == z3.SignExt(17, a) - z3.SignExt(17, b)))
    assert owner.lemmas
    for lemma in owner.lemmas:
        solver = z3.SolverFor("QF_BV")
        solver.add(z3.parse_smt2_string(lemma.query))
        assert solver.check() == z3.unsat


@pytest.mark.parametrize("signed", (False, True))
def test_overflowing_widening_is_not_admitted(signed):
    a, b = z3.BitVecs("a b", 4)
    cast = z3.SignExt if signed else z3.ZeroExt
    original = cast(4, a + b)
    owner = rewriter()
    normalized = owner.normalize(original)
    solver = z3.SolverFor("QF_BV")
    solver.add(original != normalized)
    assert solver.check() == z3.unsat
    assert not owner.lemmas
    solver = z3.SolverFor("QF_BV")
    solver.add(normalized != cast(4, a) + cast(4, b))
    assert solver.check() == z3.sat


def test_width_faithful_s16_formula_normalizes_without_integer_relaxation():
    ar, ai, br, bi = z3.BitVecs("ar ai br bi", 16)

    def signed(value, width):
        return z3.SignExt(width - value.size(), value)

    def product32(a, b):
        return signed(a, 32) * signed(b, 32)

    source = signed(product32(ar, br), 33) - signed(product32(ai, bi), 33)
    p0 = signed(ar, 48) * signed(signed(br, 17) - signed(bi, 17), 48)
    p1 = signed(signed(ar, 17) - signed(ai, 17), 48) * signed(bi, 48)
    selected = z3.Extract(
        32, 0, signed(z3.Extract(32, 0, p0), 34) + signed(z3.Extract(32, 0, p1), 34)
    )
    owner = rewriter()
    assert z3.is_true(
        z3.simplify(owner.normalize(source) == owner.normalize(selected), som=True)
    )


def test_proof_queries_are_not_keyed_by_z3_allocation_ids():
    queries = []
    for count in (1, 30):
        context = z3.Context()
        retained = [
            z3.BitVec(f"irrelevant_{index}", 7, ctx=context) for index in range(count)
        ]
        assert retained
        a, b = z3.BitVecs("a b", 16, ctx=context)
        owner = rewriter()
        owner.normalize(z3.SignExt(16, z3.SignExt(1, a) - z3.SignExt(1, b)))
        queries.append(tuple(item.query for item in owner.lemmas))
    assert queries[0] == queries[1]


def test_cache_does_not_mix_z3_contexts():
    owner = rewriter()
    owner.normalize(z3.BitVec("x", 16, ctx=z3.Context()))
    with pytest.raises(ArithmeticProofUnavailable, match="one Z3 context"):
        owner.normalize(z3.BitVec("x", 16, ctx=z3.Context()))


def test_memo_retains_ast_owners_across_temporary_expressions():
    owner = rewriter()
    for index in range(30):
        width = 16 + index % 3
        value = z3.BitVec(f"temporary_{index}", width)
        assert owner.normalize(value + value).size() == width


def test_proof_bounds_fail_closed():
    value = z3.BitVec("x", 16)
    with pytest.raises(ArithmeticProofUnavailable, match="budget"):
        CheckedBitvectorRewriter(deadline=time.monotonic() - 1).normalize(value)
    with pytest.raises(ArithmeticProofUnavailable, match="node bound"):
        rewriter(max_nodes=1).normalize(value + value)
    with pytest.raises(ArithmeticProofUnavailable, match="degree"):
        _bounded_degree(value * value * value)


def test_policy_is_not_enabled_for_scalar_or_other_solver():
    assert rtl_arithmetic_policy(None, "z3") is None
    assert rtl_arithmetic_policy(None, "boolector") is None


def test_missing_optional_python_binding_keeps_raw_formal_route(monkeypatch):
    from types import SimpleNamespace
    from importlib.metadata import PackageNotFoundError
    import zlang.formal_joint_value as policy

    def absent(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(policy, "version", absent)
    graph = SimpleNamespace(
        resources=(SimpleNamespace(configuration=(("parallel_bilinear", 1),)),)
    )
    assert policy.rtl_arithmetic_policy(graph, "z3") is None


def test_preparation_failure_is_unknown_not_a_proof(tmp_path, monkeypatch):
    import zlang.formal_rtl_arithmetic as preparation
    from zlang.ir.formal import FormalStatus, ProofMode

    def unavailable(*args, **kwargs):
        raise ArithmeticProofUnavailable("deliberate proof budget exhaustion")

    monkeypatch.setattr(preparation, "normalize_rtl_arithmetic", unavailable)
    result = preparation.run_rtl_arithmetic_formal(
        "",
        top="test",
        property_id="test",
        mode=ProofMode.PROVE,
        solver="z3",
        depth=6,
        timeout_seconds=1,
        work_directory=tmp_path,
    )
    assert result.status is FormalStatus.UNKNOWN
    assert "deliberate proof budget exhaustion" in result.reason


def test_missing_tools_respect_scoped_discovery_without_preparation(
    tmp_path, monkeypatch
):
    import zlang.formal_rtl_arithmetic as preparation
    from zlang.common.tool_inventory import ToolInventory
    from zlang.formal import FormalToolchainContext, use_formal_toolchain
    from zlang.ir.formal import FormalStatus, ProofMode

    context = FormalToolchainContext(
        "sby", "z3", ToolInventory(("yosys", "sby", "yosys-smtbmc", "z3"), ())
    )

    def unexpected(*args, **kwargs):
        pytest.fail("missing tools must not reach the RTL preparation process")

    monkeypatch.setattr(preparation, "normalize_rtl_arithmetic", unexpected)
    with use_formal_toolchain(context):
        result = preparation.run_rtl_arithmetic_formal(
            "",
            top="test",
            property_id="test",
            mode=ProofMode.PROVE,
            solver="z3",
            depth=6,
            timeout_seconds=1,
            work_directory=tmp_path,
        )
    assert result.status is FormalStatus.SKIPPED
    assert "missing formal tools" in result.reason
