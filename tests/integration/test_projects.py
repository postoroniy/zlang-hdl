from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_file
from zlang.opt import render, restore
from zlang.workspace import update_project_lock


ROOT = Path(__file__).resolve().parents[2]


def _snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
    dependency = tmp_path / "logic"
    (dependency / "src").mkdir(parents=True)
    (dependency / "zlang.toml").write_text(
        'schema=1\n[project]\nname="logic"\nversion="1"\nsource-root="src"\n'
    )
    dep_source = dependency / "src" / "identity.zl"
    dep_source.write_text("module Identity { in x:u8 out y:u8 y=x }")

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    manifest = project / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
        '[dependencies]\nlogic={path="../logic"}\n'
    )
    top = project / "src" / "top.zl"
    top.write_text(
        "import logic.identity module Top { in x:u8 out y:u8 "
        "inst child:Identity child.x=x y=child.y }"
    )
    return manifest, top, dep_source


def test_project_compilation_round_trips_identity_and_backend_artifact(tmp_path: Path) -> None:
    manifest, top, _ = _project(tmp_path)
    update_project_lock(manifest)
    before = _snapshot(tmp_path)
    result = compile_file(top, include_clash=False)
    assert _snapshot(tmp_path) == before
    assert result.ir.root_module_identity is not None
    assert result.ir.root_module_identity.logical_path == "demo.top"
    assert result.ir.dependency_closure is not None
    assert tuple(item.logical_path for item in result.ir.dependency_closure.modules) == (
        "logic.identity",
    )
    assert restore(result.optimization_ir) == result.ir
    rendered = render(result.optimization_ir)
    assert "root-module logical_path=demo.top" in rendered
    assert "dependency-closure schema=1" in rendered

    artifact = emit_artifact(result.ir)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.build_identity == artifact.build_identity
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.selected_ir_identity == artifact.selected_ir_identity
    assert restored.root_module_identity == result.ir.root_module_identity
    assert restored.dependency_closure == result.ir.dependency_closure


def test_semantic_dependency_change_invalidates_build_not_identical_rtl_hash(
    tmp_path: Path,
) -> None:
    manifest, top, dependency = _project(tmp_path)
    update_project_lock(manifest)
    first = emit_artifact(compile_file(top, include_clash=False).ir)
    dependency.write_text("// provenance-only change\nmodule Identity { in x:u8 out y:u8 y=x }")
    update_project_lock(manifest)
    second = emit_artifact(compile_file(top, include_clash=False).ir)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert first.build_identity != second.build_identity
    assert first.selected_ir_identity != second.selected_ir_identity


def test_root_package_import_participates_in_build_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    manifest = project / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    helper = project / "src" / "helper.zl"
    helper.write_text("module Helper { in x:u8 out y:u8 y=x }")
    top = project / "src" / "top.zl"
    top.write_text(
        "import demo.helper module Top { in x:u8 out y:u8 "
        "inst helper:Helper helper.x=x y=helper.y }"
    )
    update_project_lock(manifest)
    first = emit_artifact(compile_file(top, include_clash=False).ir)
    assert first.dependency_closure is not None
    assert tuple(item.logical_path for item in first.dependency_closure.modules) == (
        "demo.helper",
    )
    helper.write_text("// source-only change\nmodule Helper { in x:u8 out y:u8 y=x }")
    second = emit_artifact(compile_file(top, include_clash=False).ir)
    assert first.artifact_hash == second.artifact_hash
    assert first.build_identity != second.build_identity


def test_lock_and_compiler_cli_project_flow(tmp_path: Path) -> None:
    manifest, top, _ = _project(tmp_path)
    locked = subprocess.run(
        [
            sys.executable,
            "-m",
            "zlang.project_cli",
            "update",
            "--project",
            str(manifest.parent),
            "--verbose",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert locked.returncode == 0, locked.stderr
    assert locked.stderr == ""
    assert "2 module(s)" not in locked.stdout
    assert "1 package(s), 1 module(s)" in locked.stdout

    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "zlang.cli",
            str(top),
            "--project",
            str(manifest),
            "--check",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert "syntax and semantics valid" in checked.stdout
    assert checked.stderr == ""

    systemverilog = tmp_path / "Top.sv"
    emitted = subprocess.run(
        [
            sys.executable,
            "-m",
            "zlang.cli",
            str(top),
            "--systemverilog",
            str(systemverilog),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert emitted.returncode == 0, emitted.stderr
    assert emitted.stdout == ""
    assert "module Top" in systemverilog.read_text()


def test_project_diagnostic_is_structured_and_never_fetches(tmp_path: Path) -> None:
    manifest, top, _ = _project(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "zlang.cli",
            str(top),
            "--project",
            str(manifest),
            "--check",
            "--diagnostic-format",
            "json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(result.stderr)
    assert payload["code"] == "ZL-PROJECT-001"
    assert "lock is unavailable" in payload["message"]
    assert not (manifest.parent / ".zlang").exists()


def test_no_project_compile_file_keeps_legacy_std_only_behavior(tmp_path: Path) -> None:
    source = tmp_path / "standalone.zl"
    source.write_text("module Standalone { out y:u8 y=1 }")
    result = compile_file(source, include_clash=False)
    assert result.ir.name == "Standalone"
    assert result.ir.root_module_identity is None
    assert result.ir.dependency_closure is None
