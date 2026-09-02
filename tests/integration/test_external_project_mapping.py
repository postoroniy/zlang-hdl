from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from zlang.backend.external import ExternalMappingError, load_profile_external_mappings
from zlang.compiler import compile_file
from zlang.project import ProjectManifest, ProjectModelError
from zlang.workspace import WorkspaceError, load_project_workspace, update_project_lock

from tests.parser.test_external_modules import SOURCE


PHYSICAL_SOURCE = """module PhysicalAdd(
  input logic [7:0] lhs,
  input logic [7:0] rhs,
  output logic [8:0] result
);
  assign result = lhs + rhs;
endmodule
"""


def _project(tmp_path: Path, *, ports: str = 'a="lhs"\nb="rhs"\ny="result"') -> tuple[Path, Path, Path]:
    root = tmp_path / "external-project"
    source = root / "src" / "top.zhl"
    rtl = root / "rtl" / "physical_add.sv"
    source.parent.mkdir(parents=True)
    rtl.parent.mkdir(parents=True)
    source.write_text(SOURCE)
    rtl.write_text(PHYSICAL_SOURCE)
    manifest = root / "zlang.toml"
    manifest.write_text(
        """schema = 1
[project]
name = "acme.ext"
version = "1"
source-root = "src"

[profiles.release]
backend = "systemverilog"
backend-mode = "required"
external-mappings = ["vendor-add"]

[external-mappings.vendor-add]
logical-module = "VendorAdd"
backend = "systemverilog"
physical-module = "PhysicalAdd"
sources = ["rtl/physical_add.sv"]

[external-mappings.vendor-add.ports]
""" + ports + "\n"
    )
    update_project_lock(manifest)
    return manifest, source, rtl


def test_external_manifest_lock_round_trip_and_profile_resolution(tmp_path: Path) -> None:
    manifest_path, source, rtl = _project(tmp_path)
    manifest = ProjectManifest.load(manifest_path)
    restored = ProjectManifest.parse(manifest.render())
    assert restored.to_data() == manifest.to_data()
    workspace = load_project_workspace(source, project=manifest_path)
    assert workspace is not None
    locked = workspace.lock.external_mappings[0]
    assert locked.name == "vendor-add"
    assert locked.sources[0].digest == hashlib.sha256(rtl.read_bytes()).hexdigest()
    assert workspace.lock.identity == type(workspace.lock)(
        workspace.lock.schema,
        workspace.lock.manifest_resolution_digest,
        workspace.lock.packages,
    ).identity
    result = compile_file(source, project=manifest_path, profile="release", include_clash=False)
    mappings = load_profile_external_mappings(
        workspace.manifest, workspace.lock, "release", result.ir
    )
    assert len(mappings) == 1
    assert mappings[0].physical_module_name == "PhysicalAdd"
    assert mappings[0].port_map == (("a", "lhs"), ("b", "rhs"), ("y", "result"))


def test_external_cli_emits_pinned_source_wrapper_and_simulates(tmp_path: Path) -> None:
    manifest, source, _ = _project(tmp_path)
    output = tmp_path / "external.sv"
    artifact_path = tmp_path / "backend-artifact.json"
    command = subprocess.run(
        [
            sys.executable, "-m", "zlang.cli", str(source),
            "--project", str(manifest), "--profile", "release",
            "--systemverilog", str(output),
            "--implementation-manifest", str(artifact_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert command.returncode == 0, command.stderr
    assert command.stdout == ""
    emitted = output.read_text()
    assert PHYSICAL_SOURCE in emitted
    assert "PhysicalAdd external_impl" in emitted
    assert ".lhs(a)" in emitted and ".result(y)" in emitted
    artifact = json.loads(artifact_path.read_text())
    assert artifact["artifact_hash"] == hashlib.sha256(emitted.encode()).hexdigest()
    assert any(item["semantic_signal_id"] == "port:y" for item in artifact["bindings"])

    verilator = shutil.which("verilator")
    if verilator is None:
        pytest.skip("Verilator unavailable")
    testbench = tmp_path / "tb.sv"
    testbench.write_text(
        """module tb;
  logic [7:0] a, b;
  logic [8:0] y;
  Top dut(.a(a), .b(b), .y(y));
  initial begin
    a = 8'd255; b = 8'd1; #1;
    if (y !== 9'd256) $fatal(1, "external mismatch");
    a = 8'd7; b = 8'd9; #1;
    if (y !== 9'd16) $fatal(1, "external mismatch");
    $finish;
  end
endmodule
"""
    )
    object_dir = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    compiled = subprocess.run(
        [
            verilator, "--binary", "--timing", "-Wall", "-Wno-DECLFILENAME",
            "--top-module", "tb", "--Mdir", str(object_dir),
            str(output), str(testbench),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert compiled.returncode == 0, compiled.stderr
    simulated = subprocess.run(
        [str(object_dir / "Vtb")], check=False, capture_output=True, text=True
    )
    assert simulated.returncode == 0, simulated.stderr


def test_external_mapping_fails_closed_for_dirty_source_and_port_mismatch(tmp_path: Path) -> None:
    manifest, source, rtl = _project(tmp_path)
    rtl.write_text(PHYSICAL_SOURCE.replace("lhs + rhs", "lhs - rhs"))
    with pytest.raises(WorkspaceError, match="external mappings.*dirty"):
        load_project_workspace(source, project=manifest)

    mismatch_manifest, mismatch_source, _ = _project(
        tmp_path / "ports", ports='a="lhs"\ny="result"'
    )
    result = compile_file(
        mismatch_source,
        project=mismatch_manifest,
        profile="release",
        include_clash=False,
    )
    workspace = load_project_workspace(mismatch_source, project=mismatch_manifest)
    assert workspace is not None
    with pytest.raises(ExternalMappingError, match="ports.*must be"):
        load_profile_external_mappings(
            workspace.manifest, workspace.lock, "release", result.ir
        )


def test_external_mapping_requires_explicit_profile_selection(tmp_path: Path) -> None:
    manifest, source, _ = _project(tmp_path)
    result = compile_file(source, project=manifest, include_clash=False)
    workspace = load_project_workspace(source, project=manifest)
    assert workspace is not None
    with pytest.raises(ExternalMappingError, match="require a selected project profile"):
        load_profile_external_mappings(
            workspace.manifest, workspace.lock, None, result.ir
        )


def test_external_mapping_rejects_traversal_and_symlink_sources(tmp_path: Path) -> None:
    with pytest.raises(ProjectModelError, match="normalized relative path"):
        ProjectManifest.parse(
            """schema=1
[project]
name="acme.ext"
version="1"
source-root="src"
[external-mappings.bad]
logical-module="VendorAdd"
backend="systemverilog"
physical-module="PhysicalAdd"
sources=["../outside.sv"]
[external-mappings.bad.ports]
a="a"
"""
        )

    manifest, source, rtl = _project(tmp_path / "symlink")
    target = rtl.with_name("target.sv")
    target.write_text(rtl.read_text())
    rtl.unlink()
    rtl.symlink_to(target)
    with pytest.raises(WorkspaceError, match="must not be a symlink"):
        update_project_lock(manifest)
