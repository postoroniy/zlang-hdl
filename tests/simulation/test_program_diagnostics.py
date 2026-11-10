# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Program failure diagnostics must not expand retained hardware graphs."""

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from zlang import sim


SELECTED_IDENTITY = "selected:" + "123456789abcdef0" * 4


class _HeavyValue:
    def __repr__(self) -> str:
        raise AssertionError("heavy-field repr must not be evaluated")


@pytest.mark.parametrize(("strict", "logic"), ((False, False), (False, True), (True, True)))
def test_program_repr_preserves_identity_and_modes_without_graph_expansion(
    strict: bool, logic: bool,
) -> None:
    heavy = _HeavyValue()
    program = sim.Program(
        heavy, heavy, heavy,
        selected_ir_identity=SELECTED_IDENTITY,
        strict_uninitialized=strict,
        logic_state=logic,
        physical_inputs=heavy,
        simulation_blocks=(heavy,),
        simulation_locals=(heavy,),
    )
    text = repr(program)
    assert text.startswith("Program(")
    assert f"selected_ir_identity={SELECTED_IDENTITY!r}" in text
    assert f"strict_uninitialized={strict!r}" in text
    assert f"logic_state={logic!r}" in text
    assert len(text) <= 192


def test_compact_program_diagnostics_preserve_constructor_identity_and_equality() -> None:
    plan = SimpleNamespace(identity="native-plan-identity")
    module, native = _HeavyValue(), _HeavyValue()
    physical = _HeavyValue()
    program = sim.Program(
        plan, module, native, SELECTED_IDENTITY, True, True, physical, (), (),
    )
    assert program.plan is plan
    assert program.module is module
    assert program._native is native
    assert program.identity == "native-plan-identity"
    assert program == replace(program)
    assert program != replace(program, plan=object())
    assert program != replace(program, module=object())
    assert program != replace(program, _native=object())
    assert program != replace(program, selected_ir_identity=None)
    assert program == replace(
        program, physical_inputs=object(),
        simulation_blocks=(object(),), simulation_locals=(object(),),
    )


def test_pytest_long_traceback_retains_original_failure_and_program_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "test_program_failure.py"
    source.write_text(
        "from pathlib import Path\n"
        "from zlang import sim\n"
        f"assert Path(sim.__file__).resolve() == Path({str(Path(sim.__file__).resolve())!r})\n"
        f"SELECTED_IDENTITY = {SELECTED_IDENTITY!r}\n"
        "class Heavy:\n"
        "    def __repr__(self):\n"
        "        raise AssertionError('heavy-field repr must not be evaluated')\n"
        "def fail(program):\n"
        "    raise RuntimeError('original comparison failure retained')\n"
        "def test_failure():\n"
        "    program = sim.Program(Heavy(), Heavy(), Heavy(), SELECTED_IDENTITY, False, True)\n"
        "    fail(program)\n",
        encoding="utf-8",
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    command = (
        sys.executable, "-I", "-B", "-m", "pytest", "-q", "--color=no", "--tb=long",
        "-c", str(config), "--rootdir", str(tmp_path), str(source),
    )
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    try:
        result = subprocess.run(
            command, cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        for name, value in (("stdout", error.stdout), ("stderr", error.stderr)):
            (tmp_path / f"pytest.{name}.log").write_text(
                value.decode(errors="replace") if isinstance(value, bytes) else value or "",
                encoding="utf-8",
            )
        raise
    (tmp_path / "pytest.stdout.log").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "pytest.stderr.log").write_text(result.stderr, encoding="utf-8")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "1 failed" in output
    assert "RuntimeError: original comparison failure retained" in output
    assert SELECTED_IDENTITY in output
    assert f"program = Program(selected_ir_identity={SELECTED_IDENTITY!r}" in output
    assert "heavy-field repr must not be evaluated" not in output
    assert len(output.encode()) < 16_384
