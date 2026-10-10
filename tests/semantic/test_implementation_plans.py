from dataclasses import replace
from pathlib import Path

import pytest

import zlang.implementation_plans as implementation_plans
from zlang.compiler import compile_source
from zlang.implementation_plans import (
    BackendImplementationPlanningError,
    BackendPlanRequirement,
    BackendPlanStatus,
    plan_backend_implementations,
)
from zlang.implementation_request import BackendKind, BackendRequest, RequirementMode


ROOT = Path(__file__).resolve().parents[2]


def _add_module():
    return compile_source(
        "module Add { in a:u8 in b:u8 out y:u9 y=a+b }"
    ).ir


def _target_module():
    return compile_source(
        (ROOT / "examples/symmetric_fixed_fir_auto.zhl").read_text(),
        top="SymmetricFixedFIRAuto",
    ).ir


def test_default_plan_contains_only_the_production_backend() -> None:
    result = plan_backend_implementations(
        _add_module(), backend_requests=(("systemverilog", "required"),)
    )
    assert tuple(item.backend for item in result.plans) == ("systemverilog",)
    plan = result.plan_for("direct_systemverilog")
    assert plan.status is BackendPlanStatus.SELECTED
    assert plan.graph is not None and plan.graph.realization_backend == "backend_independent"


def test_unknown_backend_is_rejected_at_the_planning_boundary() -> None:
    with pytest.raises(ValueError, match="unsupported implementation backend"):
        plan_backend_implementations(
            _add_module(), backend_requests=(("retired_backend", "required"),)
        )


def test_compile_source_runs_default_target_planning_once(monkeypatch) -> None:
    calls = 0
    original = implementation_plans.plan_target_pipeline

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(implementation_plans, "plan_target_pipeline", counted)
    _add_module()
    assert calls == 1


def test_target_physical_graph_is_selected_for_systemverilog() -> None:
    result = plan_backend_implementations(
        _target_module(), target="xc7z030ffg676-1",
        source_policy="measured_required",
        backend_requests=(("systemverilog", "required"),),
    )
    plan = result.plan_for("systemverilog")
    assert plan.status is BackendPlanStatus.SELECTED
    assert plan.graph is not None and not plan.graph.is_generic
    assert plan.graph.realization_backend == "direct_systemverilog"


def test_required_illegal_architecture_is_fatal_and_inspectable() -> None:
    with pytest.raises(BackendImplementationPlanningError) as raised:
        plan_backend_implementations(
            _add_module(), target="xc7z030ffg676-1",
            architecture="does.not.exist", architecture_mode="required",
            backend_requests=(("systemverilog", "required"),),
        )
    plan = raised.value.result.plan_for("systemverilog")
    assert plan.status is BackendPlanStatus.UNSUPPORTED
    assert "unknown architecture" in plan.reason


def test_duck_typed_backend_request_is_supported() -> None:
    result = plan_backend_implementations(
        _add_module(),
        backend_requests=(
            BackendRequest(BackendKind.SYSTEMVERILOG, RequirementMode.REQUIRED),
        ),
    )
    assert result.plan_for("direct_systemverilog").requirement is BackendPlanRequirement.REQUIRED


def test_exact_module_timing_is_retained() -> None:
    module = compile_source("""
module Timed {
    clock clk reset rst
    in x:u8 out y:u8
    y = pipeline(4) { x }
    timing { latency 4 ii 1 }
}
""").ir
    plan = plan_backend_implementations(
        module, backend_requests=(("systemverilog", "required"),)
    ).plan_for("systemverilog")
    assert plan.graph is not None
    assert (plan.graph.latency_knowledge, plan.graph.latency,
            plan.graph.initiation_interval) == ("known", 4, 1)


def test_duplicate_backend_aliases_are_rejected() -> None:
    with pytest.raises(ValueError, match="requested more than once"):
        plan_backend_implementations(
            _add_module(),
            backend_requests=(("systemverilog", "preferred"),
                              ("direct_systemverilog", "required")),
        )


def test_plan_identity_changes_with_requirement() -> None:
    preferred = plan_backend_implementations(
        _add_module(), backend_requests=(("systemverilog", "preferred"),)
    )
    required = plan_backend_implementations(
        _add_module(), backend_requests=(("systemverilog", "required"),)
    )
    assert preferred.identity != required.identity
    assert preferred.plan_for("systemverilog").graph.identity == required.plan_for(
        "systemverilog"
    ).graph.identity


def test_generic_fallback_invariant_rejects_physical_graph() -> None:
    selected = plan_backend_implementations(
        _target_module(), target="xc7z030ffg676-1",
        source_policy="measured_required",
        backend_requests=(("systemverilog", "required"),),
    ).plan_for("systemverilog")
    with pytest.raises(ValueError, match="must carry a generic"):
        replace(selected, status=BackendPlanStatus.GENERIC_FALLBACK)
