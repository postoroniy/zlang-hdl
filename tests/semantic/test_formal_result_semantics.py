from subprocess import CompletedProcess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from zlang.backend.systemverilog import emit_artifact_with_source_map
from zlang.common.tool_inventory import ToolInventory
from zlang.compiler import compile_source
from zlang.formal import FormalToolchainContext, run_verilog_formal
from zlang.ir.formal import (
    Counterexample,
    FormalError,
    FormalResult,
    FormalStatus,
    ProofMode,
    classify_result,
)
from zlang.toolchain import GeneratedDiagnosticContext


def test_result_status_is_consistent_with_proof_mode() -> None:
    with pytest.raises(FormalError, match="bounded_pass is only valid for BMC"):
        FormalResult(
            "bounded", FormalStatus.BOUNDED_PASS, ProofMode.PROVE,
            "sby", "z3", 4,
        )
    with pytest.raises(FormalError, match="proven is only valid for prove"):
        FormalResult(
            "proven", FormalStatus.PROVEN, ProofMode.BMC,
            "sby", "z3", 4,
        )


def test_failed_result_requires_exclusive_counterexample_metadata() -> None:
    with pytest.raises(FormalError, match="require exactly one counterexample"):
        FormalResult(
            "failed", FormalStatus.FAILED, ProofMode.BMC,
            "sby", "z3", 4,
        )
    with pytest.raises(FormalError, match="require exactly one counterexample"):
        FormalResult(
            "unknown", FormalStatus.UNKNOWN, ProofMode.BMC,
            "sby", "z3", 4,
            counterexample=Counterexample("unknown", raw_trace="not valid"),
        )


def test_classifier_never_promotes_bmc_to_proven() -> None:
    with pytest.raises(FormalError, match="proven is only valid for prove"):
        classify_result(
            property_id="p", mode=ProofMode.BMC, outcome="proven",
            engine="sby", solver="z3", depth=4,
        )

    bounded = classify_result(
        property_id="p", mode=ProofMode.BMC, outcome="pass",
        engine="sby", solver="z3", depth=4,
    )
    proven = classify_result(
        property_id="p", mode=ProofMode.PROVE, outcome="pass",
        engine="sby", solver="z3", depth=4,
    )
    assert bounded.status is FormalStatus.BOUNDED_PASS
    assert proven.status is FormalStatus.PROVEN


def _run_with(
    completed: CompletedProcess[str],
    *,
    status: tuple[str, int, int] | None,
    traces: tuple[Path, ...] = (),
):
    parsed = (
        (None, "SymbiYosys status artifact is missing")
        if status is None
        else (SimpleNamespace(
            state=status[0], return_code=status[1], engine_code=status[2]
        ), None)
    )
    with patch("zlang.formal.shutil.which", return_value="/tool"), patch(
        "zlang.formal.subprocess.run", return_value=completed,
    ), patch(
        "zlang.formal._read_sby_status", return_value=parsed,
    ), patch(
        "zlang.formal._sby_trace_files", return_value=traces,
    ):
        return run_verilog_formal(
            "module top; endmodule", top="top", property_id="p", depth=2,
        )


def test_nonzero_tool_or_configuration_error_is_unknown() -> None:
    result = _run_with(CompletedProcess(
        ("sby",), 2, stdout="ERROR: parser failed before engine startup\n", stderr="",
    ), status=("ERROR", 16, 0))
    assert result.status is FormalStatus.UNKNOWN
    assert result.counterexample is None
    assert "ERROR status" in (result.reason or "")


def test_explicit_failure_is_failed_even_when_output_mentions_pass() -> None:
    result = _run_with(CompletedProcess(
        ("sby",), 2,
        stdout=(
            "Status: PASSED\n"
            "Status returned by engine: FAIL\n"
            "DONE (FAIL, rc=2)\n"
        ),
        stderr="",
    ), status=("FAIL", 2, 0), traces=(Path("trace.vcd"),))
    assert result.status is FormalStatus.FAILED
    assert result.counterexample is not None
    assert "Status returned by engine: FAIL" in (result.counterexample.raw_trace or "")


def test_log_text_cannot_replace_missing_authoritative_status() -> None:
    result = _run_with(CompletedProcess(
        ("sby",), 0,
        stdout="Status: passed\nStatus returned by engine: pass\n",
        stderr="",
    ), status=None)
    assert result.status is FormalStatus.UNKNOWN
    assert result.counterexample is None
    assert result.reason == "SymbiYosys status artifact is missing"


def test_execution_error_maps_generated_line_and_retains_raw_solver_log(
    tmp_path: Path,
) -> None:
    source = """
module MappedFormalError {
    in a : u8
    in b : u8
    out y : u9
    y = a + b
}
"""
    module = compile_source(
        source,
        include_clash=False,
        source_unit="examples/mapped_formal_error.zhl",
    ).ir
    artifact, source_map = emit_artifact_with_source_map(module)
    mapped_line = source_map.entries[0].generated.start_line
    raw_error = (
        f"ERROR: MappedFormalError_formal.v:{mapped_line}:7: "
        "backend preparation failed\n"
    )
    inventory = ToolInventory(
        ("yosys", "sby", "yosys-smtbmc", "z3"),
        ("yosys", "sby", "yosys-smtbmc", "z3"),
        (("yosys", "test"), ("sby", "test"),
         ("yosys-smtbmc", "test"), ("z3", "test")),
    )
    context = FormalToolchainContext("sby", "z3", inventory)
    work = tmp_path / "work"

    def fail_before_engine(*_args, **keywords):
        cwd = Path(keywords["cwd"])
        status = cwd / "MappedFormalError_formal" / "status"
        status.parent.mkdir(parents=True)
        status.write_text("ERROR 1 1\n", encoding="ascii")
        return CompletedProcess(("sby",), 1, stdout="", stderr=raw_error)

    with patch("zlang.formal.subprocess.run", side_effect=fail_before_engine):
        result = run_verilog_formal(
            artifact.text,
            top="MappedFormalError_formal",
            property_id="mapped.error",
            depth=2,
            work_directory=work,
            toolchain=context,
            diagnostic_sources=(
                GeneratedDiagnosticContext(source_map, artifact.text),
            ),
        )

    assert result.status is FormalStatus.UNKNOWN
    assert "ERROR status" in (result.reason or "")
    assert "ZLang origin: examples/mapped_formal_error.zhl:" in (
        result.reason or ""
    )
    assert "(operator +)" in (result.reason or "")
    assert (work / "solver.stderr.log").read_text() == raw_error
    assert "ZLang origin:" not in (work / "solver.stderr.log").read_text()
