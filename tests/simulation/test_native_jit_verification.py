"""Verification is lowered to generic primitive instrumentation before Rust."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

import zlang.sim
from zlang.simulate import VerificationAssertionError
from zlang.simulation_plan import SimulationPlan, SimulationPlanError
from tests.simulation.differential import run_differential


ROOT = Path(__file__).resolve().parents[2]


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    payload["identity"] = ""
    unsigned = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    payload["identity"] = hashlib.sha256(unsigned).hexdigest()
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _checked_source(tmp_path: Path) -> Path:
    source = tmp_path / "checked.zhl"
    source.write_text(
        """
module Checked {
    clock clk reset rst
    in ok:bit
    out observed:bit = ok
    assert stays_ok { observed }
    cover saw_ok { observed }
}
""",
        encoding="utf-8",
    )
    return source


def test_verification_plan_is_generic_bounded_instrumentation() -> None:
    programs = [
        zlang.sim.compile(
            ROOT / "examples/verification/scoped_sum.zhl",
            top="ScopedSum",
            engine="reference",
        )
        for _ in range(2)
    ]
    first, second = programs
    assert first.plan.to_bytes() == second.plan.to_bytes()
    assert first.plan.identity == second.plan.identity
    assert {node["op"] for node in first.plan.payload["nodes"]}.isdisjoint(
        {"assert", "ensure", "require", "verification_scope"}
    )
    assert {probe["kind"] for edge in first.plan.payload["edge_programs"]
            for probe in edge["probes"]} == {"check", "cover"}
    assert [event["id"] for event in first.plan.payload["events"]] == list(
        range(len(first.plan.payload["events"]))
    )


def test_requirements_and_covers_match_reference_and_native() -> None:
    traces = []
    for engine in ("reference", "jit"):
        instance = zlang.sim.load(
            ROOT / "examples/verification/scoped_sum.zhl",
            top="ScopedSum",
            engine=engine,
        )
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        observations = []
        for a, b in ((101, 1), (100, 100), (5, 6)):
            instance.set("a", a)
            instance.set("b", b)
            observations.append((instance.edge("clk"), instance.drain_events()))
        traces.append(
            (
                observations,
                instance.requirement_violations,
                instance.cover_witnesses,
            )
        )
    assert traces[0] == traces[1]
    observations, violations, witnesses = traces[0]
    assert [item[0] for item in observations] == [
        {"total": 102},
        {"total": 200},
        {"total": 11},
    ]
    assert [event.category for event in observations[0][1]] == [
        "requirement_violation"
    ]
    assert [event.category for event in observations[1][1]] == ["cover_witness"]
    assert observations[2][1] == ()
    assert [(item.requirement_name, item.cycle) for item in violations] == [
        ("legal_operands", 1)
    ]
    assert [(item.goal_name, item.cycle) for item in witnesses] == [
        ("exact_budget", 2)
    ]


def test_assertion_failure_is_source_attributed_and_does_not_commit() -> None:
    results = []
    for engine in ("reference", "jit"):
        instance = zlang.sim.load(
            ROOT / "examples/verification/rare_overflow_bug.zhl",
            top="RareOverflowBug",
            engine=engine,
        )
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        instance.set("increment", 1)
        instance.set("clear", 0)
        instance.set("key", 0xC0DE_F00D_1234_5678)
        with pytest.raises(VerificationAssertionError) as caught:
            instance.run_cycles("clk", 11)
        error = caught.value
        events = instance.drain_events()
        results.append(
            (
                error.goal_name,
                error.goal_kind.value,
                error.cycle,
                error.source_origin.span.start_line,
                error.hierarchy_path,
                instance.get("value"),
                events,
            )
        )
    assert results[0] == results[1]
    assert results[0][:6] == (
        "capacity",
        "assert",
        11,
        20,
        ("RareOverflowBug",),
        10,
    )
    assert [event.category for event in results[0][6]] == ["assertion_failure"]


def test_reset_suppresses_checks_and_cover_is_recorded_once(tmp_path: Path) -> None:
    source = _checked_source(tmp_path)
    for engine in ("reference", "jit"):
        instance = zlang.sim.load(source, top="Checked", engine=engine)
        instance.set("ok", 0)
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        assert instance.drain_events() == ()
        instance.reset("rst", asserted=False)
        instance.set("ok", 1)
        instance.edge("clk")
        instance.edge("clk")
        assert [(item.goal_name, item.cycle) for item in instance.cover_witnesses] == [
            ("saw_ok", 1)
        ]
        assert [event.category for event in instance.drain_events()] == [
            "cover_witness"
        ]


def test_child_verification_retains_hierarchical_provenance(tmp_path: Path) -> None:
    source = tmp_path / "hierarchical_checks.zhl"
    source.write_text(
        """
module Child {
    clock clk reset rst
    in step:u4
    out value:u4 = count
    reg count:u4=0
    count <- truncate<4>(count+step)
    assert bounded { value < 4 }
    cover reaches_two { value == 2 }
}
module Top {
    clock clk reset rst
    in step:u4
    out value:u4
    inst child:Child
    child.step=step
    value=child.value
}
""",
        encoding="utf-8",
    )
    traces = []
    for engine in ("reference", "jit"):
        instance = zlang.sim.load(source, top="Top", engine=engine)
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        instance.set("step", 2)
        instance.edge("clk")
        instance.edge("clk")
        cover = instance.drain_events()
        with pytest.raises(VerificationAssertionError) as caught:
            instance.edge("clk")
        failure = instance.drain_events()
        traces.append((cover, failure, caught.value.hierarchy_path))
    assert traces[0] == traces[1]
    assert traces[0][2] == ("Top", "child")
    assert [event.hierarchy_path for event in (*traces[0][0], *traces[0][1])] == [
        ("Top", "child"),
        ("Top", "child"),
    ]


def test_multi_clock_checks_sample_only_the_selected_pre_edge_domain(
    tmp_path: Path,
) -> None:
    source = tmp_path / "multi_clock_checks.zhl"
    source.write_text(
        """
module MultiClockChecks {
    clock control_clk
    reset control_rst @control_clk
    clock datapath_clk
    reset datapath_rst @datapath_clk
    out control_value:u2 @control_clk
    out datapath_value:u2 @datapath_clk
    reg control:u2 @control_clk=0
    reg datapath:u2 @datapath_clk=0
    control <- truncate<2>(control+1)
    datapath <- truncate<2>(datapath+1)
    control_value=control
    datapath_value=datapath
    assert control_bound @ control_clk { control_value < 2 }
    cover datapath_one @ datapath_clk { datapath_value == 1 }
}
""",
        encoding="utf-8",
    )
    traces = []
    for engine in ("reference", "jit"):
        instance = zlang.sim.load(source, top="MultiClockChecks", engine=engine)
        instance.reset("control_rst", asserted=True)
        instance.reset("datapath_rst", asserted=True)
        instance.edge_many(["control_clk", "datapath_clk"])
        instance.reset("control_rst", asserted=False)
        instance.reset("datapath_rst", asserted=False)
        instance.edge_many(["control_clk", "datapath_clk"])
        instance.edge("datapath_clk")
        cover = instance.drain_events()
        instance.edge("control_clk")
        before = instance.outputs()
        with pytest.raises(VerificationAssertionError) as caught:
            instance.edge_many(["datapath_clk", "control_clk"])
        traces.append(
            (
                cover,
                before,
                instance.outputs(),
                caught.value.clock,
                instance.drain_events(),
            )
        )
    assert traces[0] == traces[1]
    cover, before, after, clock, failure = traces[0]
    assert [(event.category, event.clock) for event in cover] == [
        ("cover_witness", "datapath_clk")
    ]
    assert before == after
    assert clock == "control_clk"
    assert [event.category for event in failure] == ["assertion_failure"]


def test_synchronized_reset_release_suppresses_instrumentation(tmp_path: Path) -> None:
    source = tmp_path / "async_reset_checks.zhl"
    source.write_text(
        """
module AsyncResetChecks {
    clock clk
    async reset rst @clk { polarity active_high }
    in ok:bit
    assert stays_ok { ok }
}
""",
        encoding="utf-8",
    )
    results = []
    for engine in ("reference", "jit"):
        instance = zlang.sim.load(source, top="AsyncResetChecks", engine=engine)
        instance.set("ok", 0)
        instance.reset("rst", asserted=True)
        instance.reset("rst", asserted=False)
        instance.edge("clk")
        instance.edge("clk")
        assert instance.drain_events() == ()
        with pytest.raises(VerificationAssertionError) as caught:
            instance.edge("clk")
        results.append((caught.value.cycle, instance.drain_events()))
    assert results[0] == results[1]


def test_passing_instrumented_design_matches_direct_sv(tmp_path: Path) -> None:
    trace = run_differential(
        ROOT / "examples/verification/scoped_sum.zhl",
        top="ScopedSum",
        events=(
            {
                "set": {"a": 0, "b": 0},
                "reset": {"rst": True},
                "edges": ["clk"],
            },
            {
                "set": {"a": 100, "b": 100},
                "reset": {"rst": False},
                "edges": ["clk"],
            },
            {"set": {"a": 5, "b": 6}, "edges": ["clk"]},
        ),
        directory=tmp_path / "rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv


def test_malformed_instrumentation_fails_closed_in_both_decoders() -> None:
    import _zlang_native_sim

    plan = zlang.sim.compile(
        ROOT / "examples/verification/scoped_sum.zhl",
        top="ScopedSum",
        engine="reference",
    ).plan
    invalid = deepcopy(plan.payload)
    invalid["edge_programs"][0]["probes"][0]["event"] = len(invalid["events"])
    encoded = _canonical_bytes(invalid)
    with pytest.raises(SimulationPlanError, match="instrumentation probe"):
        SimulationPlan.from_bytes(encoded)
    with pytest.raises(ValueError, match="instrumentation probe"):
        _zlang_native_sim.compile_plan_bytes(encoded)


def test_rust_runtime_has_no_zlang_verification_vocabulary() -> None:
    rust = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "native-runtime/src").glob("*.rs"))
    )
    for semantic_word in (
        "VerificationGoal",
        "assertion_failure",
        "requirement_violation",
        "cover_witness",
        "scope_id",
    ):
        assert semantic_word not in rust
