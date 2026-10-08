"""Bounded regressions for the ZTPU layout-DMA scheduler cliff."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

import zlang.sim
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.simulation_plan import build_simulation_plan
from tests.simulation.differential import run_differential


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "regressions"
CASES = (
    ("layout_dma_fsm.zhl", "LayoutDmaFsmRegression", 1),
    ("layout_dma_sticky.zhl", "LayoutDmaStickyRegression", 0),
)


def _trace(instance: object) -> list[dict[str, int]]:
    inputs = {
        "enable": 1,
        "layout_valid": 0,
        "layout_in_addr": 0x100,
        "layout_out_addr": 0x200,
        "read_ar_ready": 0,
        "read_r_valid": 0,
        "read_r_data": 0,
        "read_r_last": 0,
        "read_r_resp": 0,
        "write_aw_ready": 0,
        "write_w_ready": 0,
        "write_b_valid": 0,
        "write_b_resp": 0,
    }
    for name, value in inputs.items():
        instance.set(name, value)
    events: tuple[tuple[dict[str, int], bool | None], ...] = (
        ({}, True),
        ({}, False),
        ({"layout_valid": 1}, None),
        ({"layout_valid": 0}, None),
        ({"read_ar_ready": 1}, None),
        ({
            "read_ar_ready": 0,
            "read_r_valid": 1,
            "read_r_data": 0x12345678,
            "read_r_last": 1,
        }, None),
        ({"read_r_valid": 0, "read_r_last": 0}, None),
        ({"write_aw_ready": 1}, None),
        ({"write_aw_ready": 0, "write_w_ready": 1}, None),
        ({"write_w_ready": 0, "write_b_valid": 1}, None),
        ({"write_b_valid": 0}, None),
    )
    trace = []
    for updates, reset in events:
        for name, value in updates.items():
            instance.set(name, value)
        if reset is not None:
            instance.reset("rst", asserted=reset)
        trace.append(instance.edge("clk"))
    return trace


def _events() -> tuple[dict[str, object], ...]:
    inputs: dict[str, int] = {
        "enable": 1,
        "layout_valid": 0,
        "layout_in_addr": 0x100,
        "layout_out_addr": 0x200,
        "read_ar_ready": 0,
        "read_r_valid": 0,
        "read_r_data": 0,
        "read_r_last": 0,
        "read_r_resp": 0,
        "write_aw_ready": 0,
        "write_w_ready": 0,
        "write_b_valid": 0,
        "write_b_resp": 0,
    }
    updates: tuple[tuple[dict[str, int], bool | None], ...] = (
        ({}, True),
        ({}, False),
        ({"layout_valid": 1}, None),
        ({"layout_valid": 0}, None),
        ({"read_ar_ready": 1}, None),
        ({
            "read_ar_ready": 0,
            "read_r_valid": 1,
            "read_r_data": 0x12345678,
            "read_r_last": 1,
        }, None),
        ({"read_r_valid": 0, "read_r_last": 0}, None),
        ({"write_aw_ready": 1}, None),
        ({"write_aw_ready": 0, "write_w_ready": 1}, None),
        ({"write_w_ready": 0, "write_b_valid": 1}, None),
        ({"write_b_valid": 0}, None),
    )
    events: list[dict[str, object]] = []
    for update, reset in updates:
        inputs.update(update)
        event: dict[str, object] = {"set": dict(inputs), "edges": ("clk",)}
        if reset is not None:
            event["reset"] = {"rst": reset}
        events.append(event)
    return tuple(events)


@pytest.mark.parametrize(("fixture", "top", "inactive_wlast"), CASES)
def test_layout_dma_plan_and_direct_sv_remain_bounded(
    fixture: str,
    top: str,
    inactive_wlast: int,
) -> None:
    path = FIXTURES / fixture
    compiled = compile_source(path.read_text(), top=top, source_unit=str(path))
    plan = build_simulation_plan(compiled.ir)
    first_rtl = emit(compiled.ir)
    second_rtl = emit(compiled.ir)
    assert len(plan.payload["nodes"]) < 1_000
    assert len(plan.to_bytes()) < 100_000
    assert first_rtl == second_rtl
    assert len(first_rtl) < 10_000
    assert "assign write_w_last =" in first_rtl


@pytest.mark.parametrize(("fixture", "top", "inactive_wlast"), CASES)
def test_layout_dma_native_trace_is_deterministic(
    fixture: str,
    top: str,
    inactive_wlast: int,
) -> None:
    path = FIXTURES / fixture
    with zlang.sim.load(path, top=top, engine="native") as native:
        native_trace = _trace(native)
    with zlang.sim.load(path, top=top, engine="native") as repeated_native:
        repeated_trace = _trace(repeated_native)
    assert native_trace == repeated_trace
    assert native_trace[-1]["done"] == 1
    assert native_trace[-1]["error"] == 0
    assert native_trace[-1]["write_w_data"] == 0x12345678
    assert native_trace[-1]["write_w_last"] == inactive_wlast


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(("fixture", "top", "inactive_wlast"), CASES)
def test_layout_dma_native_matches_direct_sv_cycle_for_cycle(
    fixture: str,
    top: str,
    inactive_wlast: int,
    tmp_path: Path,
) -> None:
    path = FIXTURES / fixture
    trace = run_differential(
        path,
        top=top,
        events=_events(),
        directory=tmp_path,
        timeout=30,
    )
    assert trace.native == trace.direct_sv
    assert trace.native[-1]["done"] == 1
    assert trace.native[-1]["error"] == 0
    assert trace.native[-1]["write_w_data"] == 0x12345678
    assert trace.native[-1]["write_w_last"] == inactive_wlast
