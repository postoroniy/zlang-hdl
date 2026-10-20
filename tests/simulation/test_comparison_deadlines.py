"""Build deadlines must not consume the external simulation execution budget."""

from pathlib import Path
import subprocess
import sys

import pytest

from zlang import sim
from zlang import simulation_compare as comparison
import tests.simulation.differential as differential


@pytest.fixture
def program(tmp_path: Path):
    source = tmp_path / "constant.zhl"
    source.write_text("module Constant { out y:u8 y=0 }")
    return sim.compile(source, top="Constant")


@pytest.mark.parametrize(("build_timeout", "expected"), [(None, 30), (120, 120)])
def test_build_budget_is_separate_and_legacy_timeout_is_preserved(
    program, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_timeout, expected,
) -> None:
    calls = []
    monkeypatch.setattr(comparison, "_tool_commands", lambda *a, **k: (("compile",), ("run",)))

    def run(command, *, directory, timeout, phase):
        calls.append((command[0], timeout, phase))
        return subprocess.CompletedProcess(command, 0, "sample 0 y 0\n" if phase == "run" else "", "")

    monkeypatch.setattr(comparison, "_run_bounded", run)
    result = comparison.compare_program(
        program, ({"set": {}},), simulator="verilator",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=build_timeout,
    )
    assert calls == [("compile", expected, "compile"), ("run", 30, "run")]
    assert result.native_trace == result.rtl_trace == ({"y": 0},)


@pytest.mark.parametrize("field", ["timeout", "build_timeout"])
@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "30"])
def test_invalid_deadlines_fail_before_simulation(field: str, value) -> None:
    with pytest.raises(comparison.SimulationComparisonError, match="finite positive"):
        comparison.compare_program(None, (), simulator="verilator", **{field: value})


@pytest.mark.parametrize("phase", ["compile", "run"])
def test_expired_command_retains_phase_and_partial_tool_log(tmp_path: Path, phase: str) -> None:
    command = (sys.executable, "-u", "-c", "import time; print('tool started'); time.sleep(60)")
    with pytest.raises(comparison.SimulationComparisonError) as raised:
        comparison._run_bounded(command, directory=tmp_path, timeout=1, phase=phase)
    assert f"{phase} command exceeded 1 seconds" in str(raised.value)
    assert "tool started" in str(raised.value)
    assert "tool started" in (tmp_path / f"{phase}.log").read_text()


@pytest.mark.parametrize("failed_phase", ["compile", "run"])
def test_timeout_cannot_publish_success_or_run_after_failed_build(
    program, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_phase: str,
) -> None:
    calls = []
    monkeypatch.setattr(comparison, "_tool_commands", lambda *a, **k: (("compile",), ("run",)))

    def run(command, *, directory, timeout, phase):
        calls.append(phase)
        if phase == failed_phase:
            raise comparison.SimulationComparisonError(f"{phase} deadline expired")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(comparison, "_run_bounded", run)
    directory = tmp_path / "comparison"
    with pytest.raises(comparison.SimulationComparisonError, match="deadline expired"):
        comparison.compare_program(
            program, ({"set": {}},), simulator="verilator",
            artifact_directory=directory, timeout=30, build_timeout=120,
        )
    assert calls == (["compile"] if failed_phase == "compile" else ["compile", "run"])
    assert not (directory / "comparison.json").exists()


def test_separate_build_deadline_does_not_hide_value_mismatch(
    program, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comparison, "_tool_commands", lambda *a, **k: (("compile",), ("run",)))

    def run(command, *, directory, timeout, phase):
        return subprocess.CompletedProcess(command, 0, "sample 0 y 1\n" if phase == "run" else "", "")

    monkeypatch.setattr(comparison, "_run_bounded", run)
    directory = tmp_path / "comparison"
    with pytest.raises(comparison.SimulationComparisonError, match="simulation mismatch"):
        comparison.compare_program(
            program, ({"set": {}},), simulator="verilator",
            artifact_directory=directory, timeout=30, build_timeout=120,
        )
    assert not (directory / "comparison.json").exists()


@pytest.mark.parametrize("build_timeout", [120, 180])
def test_differential_adapter_forwards_independent_build_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_timeout: int,
) -> None:
    from types import SimpleNamespace

    program = object()
    monkeypatch.setattr(differential.sim, "compile", lambda *a, **k: program)
    seen = {}

    def compare(actual_program, events, **options):
        assert actual_program is program
        seen.update(options)
        return SimpleNamespace(plan_identity="plan", native_trace=(), rtl_trace=())

    monkeypatch.setattr(differential, "compare_program", compare)
    options = {} if build_timeout == 120 else {"build_timeout": build_timeout}
    differential.run_differential(
        tmp_path / "design.zhl", top="Top", events=(), directory=tmp_path,
        timeout=30, **options,
    )
    assert seen["timeout"] == 30
    assert seen["build_timeout"] == build_timeout
