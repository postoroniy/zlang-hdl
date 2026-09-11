"""Executable user-facing examples for safety proof, bug finding, and cover."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil

import pytest

from zlang.backend.systemverilog import emit_experimental as emit_systemverilog
from zlang.cli import main as compiler_main
from zlang.compiler import compile_source
from zlang.opt import canonical_ir_identity, lower, render, restore
from zlang.simulate import VerificationAssertionError, simulate_cycles
from zlang.toolchain import lint_with_verilator
from zlang.verification_bundle import load_verification_bundle
from zlang.verification_cli import main as verification_main


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "verification"
NAMES = ("bounded_counter", "rare_overflow_bug", "scoped_sum", "rv_buffer")
BYPASS_WORD = 0xC0DE_F00D_1234_5678
FORMAL_TOOLS = ("yosys", "sby", "yosys-smtbmc", "z3")
VERILATOR = shutil.which("verilator")


@pytest.fixture(scope="module")
def compiled_examples():
    return {
        name: compile_source(
            (EXAMPLES / f"{name}.zhl").read_text(encoding="utf-8"),
            source_unit=f"{name}.zhl",
        )
        for name in NAMES
    }




def test_counter_simulation_saturates_and_clear_wins(compiled_examples) -> None:
    module = compiled_examples["bounded_counter"].ir
    inputs = [{"increment": 1, "clear": 0}] * 13 + [
        {"increment": 1, "clear": 1},
        {"increment": 0, "clear": 0},
    ]
    outputs = simulate_cycles(module, inputs)
    assert [cycle["value"] for cycle in outputs[:11]] == [
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9,
    ]
    assert outputs[-2]["value"] == 9
    assert outputs[-1]["value"] == 0


def test_rare_bug_simulation_needs_the_bypass(compiled_examples) -> None:
    module = compiled_examples["rare_overflow_bug"].ir
    ordinary = [{"increment": 1, "clear": 0, "key": 0}] * 14
    assert max(cycle["value"] for cycle in simulate_cycles(module, ordinary)) == 9
    triggered = [{"increment": 1, "clear": 0, "key": BYPASS_WORD}] * 11
    with pytest.raises(VerificationAssertionError) as caught:
        simulate_cycles(module, triggered)
    assert caught.value.goal_name == "capacity"
    assert caught.value.cycle == 10


def test_scoped_sum_requirements_do_not_leak_to_global_goals(
    compiled_examples,
) -> None:
    module = compiled_examples["scoped_sum"].ir
    scopes = {scope.name: scope for scope in module.verification_scopes}
    assert scopes["$module"].requirements == ()
    assert {goal.name for goal in scopes["$module"].goals} == {
        "carry_preserved", "addition_commutes",
    }
    assert [item.name for item in scopes["bounded_request"].requirements] == [
        "legal_operands",
    ]
    properties = {
        item.generated_from: item.predicate
        for item in compiled_examples["scoped_sum"].formal_design.properties
        if item.predicate is not None
    }
    assert " -> " not in properties[
        "verification-assert:$module:carry_preserved"
    ].render()
    assert " -> " in properties[
        "verification-ensure:bounded_request:budget"
    ].render()


@pytest.fixture(scope="module", params=NAMES)
def verification_run(request, tmp_path_factory):
    missing = [tool for tool in FORMAL_TOOLS if not shutil.which(tool)]
    if missing:
        pytest.skip("real formal examples require: " + ", ".join(missing))
    name = request.param
    directory = tmp_path_factory.mktemp(f"verify-example-{name}")
    source = directory / f"{name}.zhl"
    source.write_text(
        (EXAMPLES / source.name).read_text(encoding="utf-8"), encoding="utf-8"
    )
    required = "proven" if name in {"bounded_counter", "scoped_sum"} else "checked"
    report_path = directory / "report.json"
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = compiler_main((
            str(source), "--verify", "--verify-require", required,
            "--verification-bundle", str(directory / "bundle"),
            "--verification-report", str(report_path),
            "--verification-format", "json",
            "--verification-work-dir", str(directory / "work"),
            "--formal-depth", "16", "--formal-timeout", "45",
        ))
    assert report_path.is_file(), stderr.getvalue()
    assert report_path.read_text(encoding="utf-8") == stdout.getvalue()
    return name, directory, status, json.loads(stdout.getvalue())


def _source_result(report, construct: str):
    return next(
        result for result in report["results"]
        if (result.get("source_origin") or {}).get("construct") == construct
    )


def _assert_raw_key_witness(work: Path) -> None:
    # The capacity goal binds count, not every DUT input. The key is therefore
    # checked in retained solver VCD history, not fabricated as a decoded
    # source-facing observation at the final (already overflowing) sample.
    bits = f"{BYPASS_WORD:064b}"
    for trace in work.rglob("*.vcd"):
        lines = trace.read_text(encoding="utf-8").splitlines()
        key_codes = {
            fields[3]
            for line in lines
            if line.startswith("$var ")
            and len(fields := line.split()) >= 6
            and fields[2] == "64" and fields[4] == "key"
        }
        if any(f"b{bits} {code}" in lines for code in key_codes):
            return
    pytest.fail("counterexample VCD did not retain the 64-bit bypass key")


def test_real_verification_examples_and_rare_bug_shallow_replay(
    verification_run,
) -> None:
    name, directory, status, report = verification_run
    assert status == (1 if name == "rare_overflow_bug" else 0)
    safety = [item for item in report["results"] if item["kind"] == "safety"]
    assert safety
    assert all(item["status"] not in {"unknown", "skipped"} for item in safety)
    assert all(item["work_directory"] for item in report["results"])
    assert {item["solver"] for item in report["results"]} == {"z3"}
    assert list((directory / "work").rglob("solver.stdout.log"))

    if name in {"bounded_counter", "scoped_sum"}:
        assert {item["status"] for item in safety} == {"proven"}
        bounded = [
            item for item in report["bounded_results"] if item["kind"] == "safety"
        ]
        assert bounded
        assert {item["status"] for item in bounded} == {"bounded_pass"}
    elif name == "rv_buffer":
        assert {item["status"] for item in safety} == {"bounded_pass"}
        for construct in ("cover backpressured", "cover transferred"):
            result = _source_result(report, construct)
            assert result["status"] == "witnessed"
            assert result["witness"]["values"]

    if name == "bounded_counter":
        assert _source_result(report, "assert capacity")["status"] == "proven"
        reached = _source_result(report, "cover reaches_capacity")
        assert reached["status"] == "witnessed"
        assert reached["witness"]["cycle"] < 16
        assert _source_result(report, "cover exceeds_capacity")["status"] == (
            "bounded_unreached"
        )
    elif name == "scoped_sum":
        for construct in (
            "assert carry_preserved", "assert addition_commutes", "ensure budget",
        ):
            assert _source_result(report, construct)["status"] == "proven"
        assert _source_result(report, "cover exact_budget")["status"] == "witnessed"
        feasibility = [
            result for result in report["results"]
            if result["property_id"].endswith(".requirements_feasible")
        ]
        assert feasibility
        assert {result["status"] for result in feasibility} == {"witnessed"}
    elif name == "rare_overflow_bug":
        failed = _source_result(report, "assert capacity")
        assert failed["status"] == "failed"
        assert failed["source_origin"]["source_unit"] == "rare_overflow_bug.zhl"
        counterexample = failed["counterexample"]
        assert counterexample["cycle"] is not None
        assert counterexample["reset_state"] == "0"
        assert counterexample["raw_trace"]
        assert dict(counterexample["values"])["register:count"] == "0b1010"
        _assert_raw_key_witness(directory / "work")

        # Replay the same immutable design at an insufficient depth: absence
        # of a shallow counterexample must remain bounded evidence, not proof.
        bundle = directory / "bundle"
        identity = load_verification_bundle(bundle).manifest.bundle_identity
        (directory / "rare_overflow_bug.zhl").unlink()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            shallow_status = verification_main((
                str(bundle), "--depth", "8", "--timeout", "45",
                "--format", "json", "--work-dir", str(directory / "shallow-work"),
            ))
        shallow = json.loads(stdout.getvalue())
        assert shallow_status == 0
        assert {result["status"] for result in shallow["results"]} == {"bounded_pass"}
        assert {result["depth"] for result in shallow["results"]} == {8}
        assert load_verification_bundle(bundle).manifest.bundle_identity == identity
