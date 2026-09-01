from dataclasses import replace
from pathlib import Path

import pytest

import zlang.implementation_plans as implementation_plans
from zlang.compiler import compile_source
from zlang.implementation_plans import (
    BackendImplementationPlan,
    BackendImplementationPlanningError,
    BackendPlanRequirement,
    BackendPlanStatus,
    plan_backend_implementations,
)
from zlang.implementation_request import BackendKind, BackendRequest, RequirementMode


ROOT = Path(__file__).resolve().parents[2]


def _add_module():
    return compile_source(
        """
module Add {
    in a : u8
    in b : u8
    out y : u9
    y = a + b
}
""",
        include_clash=False,
    ).ir


def _target_module():
    source = (ROOT / "examples" / "symmetric_fixed_fir_auto.zl").read_text()
    return compile_source(
        source,
        top="SymmetricFixedFIRAuto",
        include_clash=False,
    ).ir


def test_no_target_selects_the_same_generic_graph_for_both_backends() -> None:
    result = plan_backend_implementations(
        _add_module(),
        backend_requests=(("systemverilog", "required"), ("clash", "preferred")),
    )
    clash = result.plan_for("clash")
    systemverilog = result.plan_for("systemverilog")
    assert clash.status is BackendPlanStatus.SELECTED
    assert systemverilog.status is BackendPlanStatus.SELECTED
    assert clash.graph is systemverilog.graph
    assert clash.graph.realization_backend == "backend_independent"
    assert clash.graph.latency_knowledge == "known"
    assert (clash.graph.latency, clash.graph.initiation_interval) == (0, 1)


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


def test_backend_slots_and_report_are_deterministic_across_request_order() -> None:
    module = _add_module()
    forward = plan_backend_implementations(
        module,
        backend_requests=(("clash", "preferred"), ("systemverilog", "required")),
    )
    reverse = plan_backend_implementations(
        module,
        backend_requests=(("systemverilog", "required"), ("clash", "preferred")),
    )
    assert forward == reverse
    assert forward.identity == reverse.identity
    assert forward.report == reverse.report
    assert tuple(item.backend for item in forward.plans) == ("clash", "systemverilog")
    assert forward.to_data()["schema"] == "zlang-backend-implementation-plan-v1"


def test_unrequested_backend_is_explicit_and_does_not_carry_a_graph() -> None:
    result = plan_backend_implementations(
        _add_module(), backend_requests=(("systemverilog", "required"),)
    )
    clash = result.plan_for("clash")
    assert clash.status is BackendPlanStatus.NOT_REQUESTED
    assert clash.requirement is BackendPlanRequirement.NOT_REQUESTED
    assert clash.graph is None
    assert "backend clash: not_requested" in result.report


def test_target_physical_graph_is_selected_only_for_direct_systemverilog() -> None:
    result = plan_backend_implementations(
        _target_module(),
        backend_requests=(("clash", "preferred"), ("systemverilog", "required")),
        target="xc7z030ffg676-1",
        source_policy="measured_required",
    )
    clash = result.plan_for("clash")
    systemverilog = result.plan_for("systemverilog")
    assert clash.status is BackendPlanStatus.GENERIC_FALLBACK
    assert clash.graph.is_generic
    assert clash.graph.realization_backend == "backend_independent"
    assert systemverilog.status is BackendPlanStatus.SELECTED
    assert not systemverilog.graph.is_generic
    assert systemverilog.graph.realization_backend == "direct_systemverilog"
    assert systemverilog.graph.identity != clash.graph.identity
    assert "Clash uses the technology-independent generic fallback" in clash.reason


def test_required_clash_physical_route_fails_with_inspectable_result() -> None:
    with pytest.raises(
        BackendImplementationPlanningError,
        match="required backend implementation is unavailable",
    ) as raised:
        plan_backend_implementations(
            _target_module(),
            backend_requests=(("clash", "required"),),
            target="xc7z030ffg676-1",
            architecture_mode="required",
            source_policy="measured_required",
        )
    clash = raised.value.result.plan_for("clash")
    assert clash.status is BackendPlanStatus.UNSUPPORTED
    assert clash.graph is None
    assert "no Clash realization" in clash.reason
    assert "backend clash: unsupported" in raised.value.result.report


def test_preferred_illegal_architecture_reports_generic_fallback() -> None:
    result = plan_backend_implementations(
        _add_module(),
        backend_requests=(("systemverilog", "preferred"),),
        target="xc7z030ffg676-1",
        architecture="does.not.exist",
        architecture_mode="preferred",
    )
    plan = result.plan_for("systemverilog")
    assert plan.status is BackendPlanStatus.GENERIC_FALLBACK
    assert plan.graph.is_generic
    assert "preferred architecture rejected" in plan.reason
    assert "unknown architecture" in plan.reason


def test_required_illegal_architecture_is_unsupported_and_fatal() -> None:
    with pytest.raises(BackendImplementationPlanningError) as raised:
        plan_backend_implementations(
            _add_module(),
            backend_requests=(("systemverilog", "required"),),
            target="xc7z030ffg676-1",
            architecture="does.not.exist",
            architecture_mode="required",
        )
    plan = raised.value.result.plan_for("systemverilog")
    assert plan.status is BackendPlanStatus.UNSUPPORTED
    assert "unknown architecture" in plan.reason


def test_duck_typed_profile_backend_request_is_supported() -> None:
    result = plan_backend_implementations(
        _add_module(),
        backend_requests=(BackendRequest(BackendKind.SYSTEMVERILOG, RequirementMode.REQUIRED),),
    )
    assert result.plan_for("direct_systemverilog").requirement is BackendPlanRequirement.REQUIRED


def test_exact_module_timing_is_retained_in_each_generic_backend_plan() -> None:
    module = compile_source(
        """
module Timed {
    clock clk
    reset rst
    in x : u8
    out y : u8
    y = pipeline(4) { x }
    timing { latency 4 ii 1 }
}
""",
        include_clash=False,
    ).ir
    result = plan_backend_implementations(
        module,
        backend_requests=(("clash", "required"), ("systemverilog", "required")),
    )
    assert {
        (item.graph.latency_knowledge, item.graph.latency, item.graph.initiation_interval)
        for item in result.plans
    } == {("known", 4, 1)}


def test_plan_invariant_rejects_direct_systemverilog_graph_on_clash() -> None:
    selected = plan_backend_implementations(
        _target_module(),
        backend_requests=(("systemverilog", "required"),),
        target="xc7z030ffg676-1",
        source_policy="measured_required",
    ).plan_for("systemverilog")
    with pytest.raises(ValueError, match="cannot be attached to Clash"):
        BackendImplementationPlan(
            backend="clash",
            requirement=BackendPlanRequirement.PREFERRED,
            status=BackendPlanStatus.SELECTED,
            graph=selected.graph,
        )


def test_duplicate_backend_aliases_are_rejected() -> None:
    with pytest.raises(ValueError, match="requested more than once"):
        plan_backend_implementations(
            _add_module(),
            backend_requests=(
                ("systemverilog", "preferred"),
                ("direct_systemverilog", "required"),
            ),
        )


def test_plan_identity_changes_when_backend_requirement_changes() -> None:
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
        _target_module(),
        backend_requests=(("systemverilog", "required"),),
        target="xc7z030ffg676-1",
        source_policy="measured_required",
    ).plan_for("systemverilog")
    with pytest.raises(ValueError, match="must carry a generic"):
        replace(selected, status=BackendPlanStatus.GENERIC_FALLBACK)
