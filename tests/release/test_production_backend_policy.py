from __future__ import annotations

from dataclasses import fields
import importlib.util
import inspect
from pathlib import Path

import pytest

from zlang.cli import main
from zlang.compilation_products import CompilationResult
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


def test_retired_backend_has_no_importable_or_python_source_surface() -> None:
    assert importlib.util.find_spec("zlang.backend.clash") is None
    production = tuple((ROOT / "zlang").rglob("*.py"))
    offenders = tuple(
        path.relative_to(ROOT)
        for path in production
        if "clash" in path.read_text(encoding="utf-8").lower()
    )
    assert offenders == ()


def test_compiler_result_and_entry_point_are_direct_only() -> None:
    assert "include_clash" not in inspect.signature(compile_source).parameters
    assert "clash" not in {item.name for item in fields(CompilationResult)}
    result = compile_source("module DirectOnly { out y:u1 y=0 }")
    assert tuple(item.backend for item in result.backend_implementation_plans.plans) == (
        "systemverilog",
    )


def test_public_help_has_no_retired_backend_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(("--help",))
    assert caught.value.code == 0
    help_text = capsys.readouterr().out.lower()
    assert "clash" not in help_text
    assert "--verilog-dir" not in help_text
    assert "--output" not in help_text


def test_release_policy_and_default_ci_have_no_retired_backend_escape_hatch() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    ).lower()
    releasing = (ROOT / "RELEASING.md").read_text(encoding="utf-8").lower()
    assert "clash" not in workflows
    assert "two complete parallel pytest runs with zero skips" in releasing
    assert "compatibility tests may skip" not in releasing
