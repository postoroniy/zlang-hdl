from __future__ import annotations


import pytest

import zlang.compilation_session as session_module
from zlang import (
    CompilationSession as ExportedCompilationSession,
    create_file_compilation_session,
    create_file_compilation_session_snapshot,
)
from zlang.cli import main
from zlang.compilation_session import (
    COMPILATION_PRODUCT_DEPENDENCIES,
    CompilationSession,
)
from zlang.compiler import (
    TopSelectionError,
    compile_file,
    compile_file_snapshot,
    compile_source,
)
from zlang.ir import expressions as ir_expr
from zlang.source_identity import SourceExtensionError
from zlang.semantic import SemanticError

SOURCE = "module Add { in a:u8 in b:u8 out y:u9 y=a+b }"


def _forbidden(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"semantic-only demand executed {name}")

    return fail


def test_semantic_demand_has_an_explicit_bounded_dependency_frontier(
    monkeypatch,
) -> None:
    for name in (
        "plan_backend_implementations",
        "build_formal_design",
        "build_recursive_formal_design",
        "emit_harness",
        "emit_recursive_harness",
        "render_implementation_report",
        "render_cost_report",
        "render_pipeline_report",
        "render_architecture_report",
        "render_exploration_report",
        "emit_csr_markdown",
        "emit_csr_json",
        "emit_contracts",
    ):
        monkeypatch.setattr(session_module, name, _forbidden(name))

    verifier_calls = 0

    def verifier(*args, **kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("check-only demand executed formal verification")

    session = CompilationSession(
        "module E { in a:u8 out y:u8 y=implement { a intent { minimize lut } } }",
        formal_policy="required_bmc",
        formal_verifier=verifier,
    )
    assert session.check().name == "E"
    assert session.computed_products == ("syntax", "semantic")
    assert session.failed_products == ()
    assert verifier_calls == 0


def test_products_and_failures_are_memoized_once_per_session(monkeypatch) -> None:
    counts = {"parse": 0, "analyze": 0, "planning": 0}

    def counted(name, original):
        def call(*args, **kwargs):
            counts[name] += 1
            return original(*args, **kwargs)

        return call

    monkeypatch.setattr(session_module, "parse", counted("parse", session_module.parse))
    monkeypatch.setattr(
        session_module, "analyze", counted("analyze", session_module.analyze)
    )
    monkeypatch.setattr(
        session_module,
        "plan_backend_implementations",
        counted("planning", session_module.plan_backend_implementations),
    )
    session = CompilationSession(SOURCE)
    assert session.semantic_ir is session.semantic_ir
    assert session.materialize() is session.materialize()
    assert counts == {"parse": 1, "analyze": 1, "planning": 1}

    broken_calls = 0

    def broken_parse(source):
        nonlocal broken_calls
        broken_calls += 1
        raise ValueError("stable failure")

    monkeypatch.setattr(session_module, "parse", broken_parse)
    broken = CompilationSession("not source")
    with pytest.raises(ValueError) as first:
        broken.check()
    with pytest.raises(ValueError) as second:
        broken.check()
    assert first.value is second.value
    assert broken_calls == 1
    assert broken.failed_products == ("syntax", "semantic")


def test_inline_locals_preserves_shared_expression_dag() -> None:
    module = compile_source(
        "module Shared { in a,b:u8 out y:u11 "
        "combined:u9=a+b y=extend<10>(combined)+extend<10>(combined) }"
    ).ir

    inlined = session_module.inline_locals(module)
    addition = inlined.assignments[0].expression

    assert isinstance(addition, ir_expr.Add)
    assert isinstance(addition.left, ir_expr.Extend)
    assert isinstance(addition.right, ir_expr.Extend)
    assert addition.left.expression is addition.right.expression


def test_sessions_snapshot_options_and_do_not_share_state() -> None:
    evidence = [object()]
    first = CompilationSession(SOURCE, target_evidence=evidence)
    evidence.append(object())
    assert first.options.target_evidence is not None
    assert len(first.options.target_evidence) == 1
    with pytest.raises(AttributeError, match="read-only"):
        first.target = "generic"
    with pytest.raises(AttributeError, match="read-only"):
        first.source = "module Other {}"

    second = CompilationSession(SOURCE)
    assert first.check() == second.check()
    assert first.computed_products == ("syntax", "semantic")
    assert second.computed_products == ("syntax", "semantic")
    assert first._values is not second._values


def test_session_snapshot_reuses_computed_phase_objects_without_new_demands() -> None:
    session = CompilationSession(SOURCE)
    semantic = session.semantic_ir

    snapshot = session.snapshot()

    assert snapshot.computed_products == ("syntax", "semantic")
    assert snapshot.syntax is session.syntax
    assert snapshot.semantic is semantic
    assert snapshot.selected is None
    assert snapshot.planned is None
    assert snapshot.simulation_plan is None
    assert snapshot.semantic_identity is not None
    assert session.computed_products == ("syntax", "semantic")


def test_stdlib_physical_inputs_survive_semantic_failure() -> None:
    session = CompilationSession(
        "import std.math.complex\nmodule Bad { in x:u8 out y:u7 y=x }"
    )
    with pytest.raises(SemanticError, match="cannot assign u8 expression"):
        session.check()
    assert any(
        path.name == "complex.zhl" for path in session.physical_inputs.stdlib_sources
    )




def test_top_diagnostic_type_and_text_remain_compatible() -> None:
    with pytest.raises(TopSelectionError) as raised:
        compile_source(SOURCE, top="Missing")
    assert str(raised.value) == "top module 'Missing' was not found"


def test_file_facades_preserve_top_diagnostic_type_and_text(tmp_path) -> None:
    source = tmp_path / "add.zhl"
    source.write_text(SOURCE)
    for compile_call in (
        lambda: compile_file(source, top="Missing"),
        lambda: compile_file_snapshot(source, SOURCE, top="Missing"),
    ):
        with pytest.raises(TopSelectionError) as raised:
            compile_call()
        assert str(raised.value) == "top module 'Missing' was not found"


def test_file_facades_reject_noncanonical_source_extensions(tmp_path) -> None:
    for suffix in (".zl", ".zlang"):
        legacy = tmp_path / f"add{suffix}"
        legacy.write_text(SOURCE)
        for compile_call in (
            lambda legacy=legacy: compile_file(legacy),
            lambda legacy=legacy: compile_file_snapshot(legacy, SOURCE),
        ):
            with pytest.raises(
                SourceExtensionError,
                match="rename the file to 'add.zhl'",
            ):
                compile_call()


def test_cli_check_demands_only_semantics(tmp_path, monkeypatch, capsys) -> None:
    source = tmp_path / "add.zhl"
    source.write_text(SOURCE)
    for name in (
        "plan_backend_implementations",
        "build_formal_design",
        "render_implementation_report",
    ):
        monkeypatch.setattr(session_module, name, _forbidden(name))
    assert main([str(source), "--check"]) == 0
    assert "syntax and semantics valid" in capsys.readouterr().out


def test_dependency_dag_is_complete_and_acyclic() -> None:
    seen: set[str] = set()
    active: set[str] = set()

    def visit(name: str) -> None:
        assert name in COMPILATION_PRODUCT_DEPENDENCIES
        if name in seen:
            return
        assert name not in active
        active.add(name)
        for dependency in COMPILATION_PRODUCT_DEPENDENCIES[name]:
            visit(dependency)
        active.remove(name)
        seen.add(name)

    for product in COMPILATION_PRODUCT_DEPENDENCIES:
        visit(product)
    assert seen == set(COMPILATION_PRODUCT_DEPENDENCIES)


def test_file_session_factories_are_public_and_lazy(tmp_path) -> None:
    source = tmp_path / "add.zhl"
    source.write_text(SOURCE)
    from_file = create_file_compilation_session(source)
    from_snapshot = create_file_compilation_session_snapshot(source, SOURCE)
    assert isinstance(from_file, ExportedCompilationSession)
    assert isinstance(from_snapshot, ExportedCompilationSession)
    assert from_file.computed_products == from_snapshot.computed_products == ()
    assert from_file.physical_inputs.root_source == source.resolve()
    assert from_snapshot.physical_inputs.root_source == source.resolve()


def test_public_policy_and_plan_properties_demand_declared_nodes_only() -> None:
    policy_session = CompilationSession(SOURCE)
    assert policy_session.implementation_policy.request == (
        policy_session.implementation_request
    )
    assert policy_session.computed_products == (
        "syntax",
        "semantic",
        "configured_semantic",
        "selection",
    )
    assert policy_session.backend_implementation_plans is (
        policy_session.backend_implementation_plans
    )
    assert policy_session.computed_products == (
        "syntax",
        "semantic",
        "configured_semantic",
        "selection",
        "planning",
    )
