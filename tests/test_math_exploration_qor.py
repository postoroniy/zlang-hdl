from __future__ import annotations

import pytest

from tools.math_exploration_qor import (
    SOURCE,
    TOPS,
    build_shell,
    build_tcl,
    build_xdc,
    digest,
    parse_metrics,
    validate_route_reports,
)
from zlang.compiler import compile_source
from zlang.timing import timing_info


def test_math_timing_shells_share_the_same_register_contract() -> None:
    source = SOURCE.read_text()
    shells = []
    latencies = []
    for top in TOPS:
        compiled = compile_source(source, top=top)
        shell = build_shell(compiled.ir)
        shells.append(shell.replace(f"{top} core", "Kernel core"))
        assignment = next(item for item in compiled.ir.assignments if item.target.name == "y")
        latencies.append(timing_info(assignment.expression, module=compiled.ir).latency)
        assert shell.count("always_ff @(posedge clk)") == 1
        assert "y <= core_result;" in shell
        assert "launch_a0 <= a0;" in shell
        assert "launch_b7 <= b7;" in shell
        assert "if (rst) begin" in shell
    assert shells[0] == shells[1] == shells[2]
    assert latencies == [0, 0, 4]


def test_timing_flow_has_identical_constraints_and_no_data_exceptions() -> None:
    script = build_tcl(10.0, "xc7z030ffg676-1")
    assert script.index("read_xdc timing.xdc") < script.index("synth_design")
    assert "-mode out_of_context -flatten_hierarchy none" in script
    assert "set_param general.maxThreads 2" in script
    assert "get_timing_paths -setup -from $regs -to $regs" in script
    assert "report_timing_summary -delay_type min_max -report_unconstrained" in script
    assert "check_timing -verbose" in script
    assert "version -short" in script
    for prohibited in ("set_false_path", "set_multicycle_path", "-retiming", "use_dsp", "cascade_dsp"):
        assert prohibited not in script
    xdc = build_xdc(10.0)
    assert "create_clock -name clk -period 10.0" in xdc
    assert "set_input_delay 0.0" in xdc
    assert "set_output_delay 0.0" in xdc


def _metrics(wns: str = "-0.5", whs: str = "0.1", clocks: str = "1") -> str:
    return f"""tool_version=2024.2
lut=10
ff=30
dsp=8
bram=0
wns_ns={wns}
whs_ns={whs}
critical_delay_proxy_ns=10.5
fmax_proxy_mhz=95.238
register_to_register_wns_ns=-0.5
critical_datapath_delay_ns=9.9
critical_startpoint=core/start/Q
critical_endpoint=core/end/D
clock_count={clocks}
"""


def test_negative_setup_slack_is_a_timing_failure() -> None:
    row = parse_metrics(_metrics())
    assert row["setup_pass"] is False
    assert row["hold_pass"] is True
    assert row["timing_pass"] is False
    assert row["wns_ns"] == -0.5
    assert row["critical_startpoint"] == "core/start/Q"


def test_hold_failure_cannot_be_reported_as_a_timing_pass() -> None:
    row = parse_metrics(_metrics(wns="0.5", whs="-0.1"))
    assert row["setup_pass"] is True
    assert row["hold_pass"] is False
    assert row["timing_pass"] is False
    assert parse_metrics(_metrics(wns="0.5"))["timing_pass"] is True


@pytest.mark.parametrize("kwargs", [{"wns": "nan"}, {"whs": "inf"}, {"clocks": "0"}])
def test_invalid_timing_evidence_is_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        parse_metrics(_metrics(**kwargs))


def test_evidence_hashes_exact_bytes_including_newline() -> None:
    assert digest("kernel\n") != digest("kernel")
    assert digest("kernel\n") == digest("kernel\n")


def test_route_validation_rejects_unconstrained_or_unrouted_results(tmp_path) -> None:
    (tmp_path / "route_status.rpt").write_text("# of nets with routing errors.......... : 0\n")
    good = "checking unconstrained_internal_endpoints (0)\nchecking no_clock (0)\n"
    (tmp_path / "check_timing.rpt").write_text(good)
    assert validate_route_reports(tmp_path) == {
        "routing_errors": 0, "unconstrained_internal_endpoints": 0,
        "unclocked_registers": 0,
    }
    (tmp_path / "check_timing.rpt").write_text(good.replace("endpoints (0)", "endpoints (1)"))
    with pytest.raises(ValueError, match="unconstrained_internal_endpoints"):
        validate_route_reports(tmp_path)
    (tmp_path / "check_timing.rpt").write_text(good)
    (tmp_path / "route_status.rpt").write_text("# of nets with routing errors.......... : 1\n")
    with pytest.raises(ValueError, match="routing_errors"):
        validate_route_reports(tmp_path)
