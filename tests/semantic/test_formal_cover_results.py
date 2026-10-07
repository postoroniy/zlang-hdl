from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from zlang.formal import emit_cover_sby, run_verilog_cover
from zlang.ir.formal import (
    CoverProperty,
    CoverResult,
    CoverStatus,
    CoverWitness,
    FormalDesign,
    FormalError,
    FormalProperty,
    Ownership,
    PropertyKind,
    SignalBinding,
    TemporalForm,
    cover_harness_top,
    emit_cover_harness,
)
from zlang.ir.formal_predicates import (
    Binary,
    Constant,
    FormalBinaryOperator,
    FormalSignedness,
    ObservationRef,
)


def _predicate():
    count = ObservationRef("register:count", 2, FormalSignedness.UNSIGNED)
    return Binary(
        FormalBinaryOperator.EQUAL,
        count,
        Constant(2, 2, FormalSignedness.UNSIGNED),
        1,
        FormalSignedness.BIT,
    )


def _cover() -> CoverProperty:
    return CoverProperty(
        "cover.count.two",
        "clk",
        "rst",
        "count == 2",
        _predicate(),
    )


def _connected_design() -> FormalDesign:
    cover = _cover()
    ports = (
        SignalBinding("clock", "Counter", "clk", 1, "input", "clk"),
        SignalBinding("reset", "Counter", "rst", 1, "input", "clk"),
        SignalBinding("port:y", "Counter", "y", 2, "output", "clk"),
    )
    bindings = ports[:2] + (
        SignalBinding(
            "register:count", "Counter", "zlang_formal_obs_count", 2,
            "output", "clk",
        ),
    )
    rtl = """module Counter(input wire clk, input wire rst, output wire [1:0] y,
  output wire [1:0] zlang_formal_obs_count);
  reg [1:0] count;
  always @(posedge clk) begin
    if (rst) count <= 0;
    else count <= count + 1;
  end
  assign y = count;
  assign zlang_formal_obs_count = count;
endmodule
"""
    return FormalDesign(
        "Counter",
        (),
        bindings,
        covers=(cover,),
        connected_backend="systemverilog",
        connected_artifact_hash="artifact",
        connected_module="Counter",
        implementation_text=rtl,
        dut_ports=ports,
    )


def test_cover_property_and_result_invariants_are_strict() -> None:
    cover = _cover()
    assert cover.relevant_signals == ("register:count",)
    with pytest.raises(FormalError, match="relevant_signals"):
        CoverProperty(
            cover.id, cover.clock, cover.reset_condition, cover.expression,
            cover.predicate, relevant_signals=("wrong",),
        )
    with pytest.raises(FormalError, match="requires witness"):
        CoverResult("p", CoverStatus.WITNESSED, "sby", "z3", 4)
    with pytest.raises(FormalError, match="only witnessed"):
        CoverResult(
            "p", CoverStatus.BOUNDED_UNREACHED, "sby", "z3", 4,
            witness=CoverWitness("p", 2),
        )
    with pytest.raises(FormalError, match="does not match"):
        CoverResult(
            "p", CoverStatus.WITNESSED, "sby", "z3", 4,
            witness=CoverWitness("other", 2),
        )
    with pytest.raises(FormalError, match="requires a depth"):
        CoverResult("p", CoverStatus.BOUNDED_UNREACHED, "sby", "z3", None)


def test_connected_cover_harness_and_sby_are_deterministic() -> None:
    design = _connected_design()
    assumption_predicate = Binary(
        FormalBinaryOperator.NOT_EQUAL,
        ObservationRef("register:count", 2, FormalSignedness.UNSIGNED),
        Constant(3, 2, FormalSignedness.UNSIGNED),
        1,
        FormalSignedness.BIT,
    )
    design = replace(
        design,
        properties=(FormalProperty(
            "assume.count.not-three",
            PropertyKind.ASSUMPTION,
            "clk",
            "rst",
            "count != 3",
            TemporalForm.SAME_CYCLE,
            Ownership.ENVIRONMENT,
            predicate=assumption_predicate,
        ),),
    )
    first = emit_cover_harness(design, cover_id="cover.count.two", depth=6)
    second = emit_cover_harness(design, cover_id="cover.count.two", depth=6)
    top = cover_harness_top(design, "cover.count.two")
    assert first == second
    assert f"module {top}" in first
    assert "cover (" in first
    assert "assert (" not in first
    assert "// assume.count.not-three" in first
    assert "zlang_formal_obs_count" in first
    config = emit_cover_sby(
        design, cover_id="cover.count.two", depth=6,
        source_file="counter_cover.sv",
    )
    assert "mode cover" in config
    assert "depth 6" in config
    assert f"prep -top {top}" in config
    assert "counter_cover.sv" in config

    unavailable = replace(
        design,
        properties=(replace(
            design.properties[0],
            non_executable_reason="missing environment binding",
        ),),
    )
    report = emit_cover_harness(
        unavailable, cover_id="cover.count.two", depth=6,
    )
    assert "non-executable property report" in report
    assert "requires executable assumption" in report
    with pytest.raises(FormalError, match="requires executable assumption"):
        emit_cover_sby(unavailable, cover_id="cover.count.two", depth=6)


def test_cover_runner_classifies_from_status_and_vcd_not_log_wording(
    tmp_path: Path,
) -> None:
    reached_output = "solver completed successfully without stable English prose\n"
    unreached_output = """
Unreached cover statement at top: top.v:1.1-1.2
Status: failed
Status returned by engine: FAIL
DONE (FAIL, rc=2)
"""
    context = SimpleNamespace(
        engine="sby", solver="z3", missing=(), versions=(),
    )
    trace = tmp_path / "trace.vcd"
    trace.write_text("""$scope module top $end
$var integer 32 ! smt_step $end
$upscope $end
$enddefinitions $end
#0
b00000000000000000000000000000000 !
#10
b00000000000000000000000000000011 !
#20
b00000000000000000000000000000100 !
""")
    with patch(
        "zlang.formal.FormalToolchainContext.discover", return_value=context,
    ), patch(
        "zlang.formal.subprocess.run",
        side_effect=(
            CompletedProcess(("sby",), 0, reached_output, ""),
            CompletedProcess(("sby",), 2, unreached_output, ""),
        ),
    ), patch(
        "zlang.formal._read_sby_status",
        side_effect=(
            (SimpleNamespace(state="PASS", return_code=0, engine_code=0), None),
            (SimpleNamespace(state="FAIL", return_code=2, engine_code=0), None),
        ),
    ), patch(
        "zlang.formal._sby_trace_files", return_value=(trace,),
    ):
        witnessed = run_verilog_cover(
            "module top; endmodule", top="top", property_id="cover.p", depth=4,
        )
        bounded = run_verilog_cover(
            "module top; endmodule", top="top", property_id="cover.p", depth=4,
        )
    assert witnessed.status is CoverStatus.WITNESSED
    assert witnessed.witness is not None
    assert witnessed.witness.cycle == 3
    assert "without stable English prose" in (witnessed.witness.raw_trace or "")
    assert bounded.status is CoverStatus.BOUNDED_UNREACHED
    assert bounded.witness is None
    assert "depth 4" in (bounded.reason or "")


def test_cover_runner_rejects_pass_with_malformed_witness_vcd(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "malformed.vcd"
    trace.write_text("$enddefinitions $end\n")
    context = SimpleNamespace(
        engine="sby", solver="z3", missing=(), versions=(),
    )
    with patch(
        "zlang.formal.FormalToolchainContext.discover", return_value=context,
    ), patch(
        "zlang.formal.subprocess.run",
        return_value=CompletedProcess(("sby",), 0, "PASS", ""),
    ), patch(
        "zlang.formal._read_sby_status",
        return_value=(SimpleNamespace(state="PASS", return_code=0, engine_code=0), None),
    ), patch(
        "zlang.formal._sby_trace_files", return_value=(trace,),
    ):
        result = run_verilog_cover(
            "module top; endmodule", top="top", property_id="cover.p", depth=4,
        )
    assert result.status is CoverStatus.UNKNOWN
    assert "malformed cover witness trace" in (result.reason or "")


def test_cover_runner_missing_tools_is_explicit_skip() -> None:
    with patch("zlang.formal.shutil.which", return_value=None):
        result = run_verilog_cover(
            "module top; endmodule", top="top", property_id="cover.p", depth=4,
        )
    assert result.status is CoverStatus.SKIPPED
    assert "missing formal tools" in (result.reason or "")
