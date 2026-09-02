"""Nonsemantic file-input tracking and CLI overwrite prevention."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path

import pytest

from zlang.cli import main
from zlang.compiler import compile_file
from zlang.workspace import update_project_lock


ROOT = Path(__file__).resolve().parents[2]


def _project(root: Path) -> tuple[Path, Path, Path, Path]:
    dependency = root / "logic"
    (dependency / "src").mkdir(parents=True)
    dependency_manifest = dependency / "zlang.toml"
    dependency_manifest.write_text(
        'schema=1\n[project]\nname="logic"\nversion="1"\nsource-root="src"\n',
        encoding="utf-8",
    )
    dependency_source = dependency / "src" / "identity.zhl"
    dependency_source.write_text(
        "module Identity { in x:u8 out y:u8 y=x }\n",
        encoding="utf-8",
    )

    project = root / "project"
    (project / "src").mkdir(parents=True)
    manifest = project / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
        '[dependencies]\nlogic={path="../logic"}\n',
        encoding="utf-8",
    )
    source = project / "src" / "top.zhl"
    source.write_text(
        "import std.bus.reg import logic.identity "
        "module Top { in x:u8 out y:u8 "
        "inst child:Identity child.x=x y=child.y }\n",
        encoding="utf-8",
    )
    update_project_lock(manifest)
    return manifest, source, dependency_manifest, dependency_source


def _invoke_collision(source: Path, project: Path, arguments: list[str]) -> str:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        with pytest.raises(SystemExit) as raised:
            main([str(source), "--project", str(project), *arguments])
    assert raised.value.code == 2
    assert stdout.getvalue() == ""
    assert "collides with compilation input" in stderr.getvalue()
    return stderr.getvalue()


def test_file_compilation_exposes_inputs_without_affecting_content_identity(
    tmp_path: Path,
) -> None:
    first_manifest, first_source, first_dep_manifest, first_dep_source = _project(
        tmp_path / "checkout-a"
    )
    second_manifest, second_source, _, _ = _project(
        tmp_path / "relocated" / "checkout-b"
    )

    first = compile_file(
        first_source, project=first_manifest, include_clash=False,
    )
    second = compile_file(
        second_source, project=second_manifest, include_clash=False,
    )
    inputs = first.physical_inputs

    assert inputs.root_source == first_source.resolve()
    assert inputs.project_manifest == first_manifest.resolve()
    assert inputs.project_lock == (first_manifest.parent / "zlang.lock").resolve()
    assert first_source.resolve() in inputs.project_module_sources
    assert first_dep_manifest.resolve() in inputs.dependency_manifests
    assert first_dep_source.resolve() in inputs.dependency_module_sources
    assert (ROOT / "stdlib" / "bus" / "reg.zhl").resolve() in inputs.stdlib_sources
    assert first.high_level_ir_identity == second.high_level_ir_identity
    assert first.selected_ir_identity == second.selected_ir_identity
    assert first.physical_inputs.all_paths != second.physical_inputs.all_paths
    assert not hasattr(first.ir, "physical_inputs")


@pytest.mark.parametrize(
    "input_name",
    ("project_manifest", "project_lock", "dependency_manifest", "dependency_source", "stdlib"),
)
def test_cli_rejects_file_sink_aliasing_any_compilation_input(
    tmp_path: Path, input_name: str,
) -> None:
    manifest, source, dependency_manifest, dependency_source = _project(tmp_path)
    paths = {
        "project_manifest": manifest,
        "project_lock": manifest.parent / "zlang.lock",
        "dependency_manifest": dependency_manifest,
        "dependency_source": dependency_source,
        "stdlib": ROOT / "stdlib" / "bus" / "reg.zhl",
    }
    protected = paths[input_name]
    original = protected.read_bytes()

    diagnostic = _invoke_collision(
        source,
        manifest,
        ["--systemverilog", str(protected)],
    )

    assert str(protected.resolve()) in diagnostic
    assert protected.read_bytes() == original


def test_cli_rejects_output_directory_containing_dependency_input(
    tmp_path: Path,
) -> None:
    manifest, source, _, dependency_source = _project(tmp_path)
    output_directory = dependency_source.parent
    original = dependency_source.read_bytes()

    diagnostic = _invoke_collision(
        source,
        manifest,
        ["--verilog-dir", str(output_directory)],
    )

    assert "explicit output directory --verilog-dir" in diagnostic
    assert dependency_source.read_bytes() == original
