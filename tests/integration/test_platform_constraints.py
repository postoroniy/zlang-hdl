from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
import io
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.common import stable_digest
from zlang.cli import main
from zlang.compiler import compile_source
from zlang.implementation_request import ImplementationRequestError, parse_selected_profile
from zlang.ir.equivalence import SignalRole
from zlang.platform_constraints import (
    ConstraintArtifact,
    ConstraintFormat,
    PlatformConstraintError,
    build_constraint_artifact,
    parse_platform_profile,
    publish_constraint_artifact,
)
from zlang.project import ProjectManifest
from zlang.workspace import update_project_lock


SOURCE = """
module Timed {
  clock clk { edge falling }
  reset rst @clk {
    mode asynchronous
    polarity active_low
    power_up unspecified
  }
  in x : u8
  out y : u8
  reg value : u8 = 0
  rule capture when 1 { value <- x }
  y = value
}
"""


def _manifest(platform: str) -> ProjectManifest:
    return ProjectManifest.parse(
        'schema=1\n[project]\nname="constraints"\nversion="1"\n'
        'source-root="src"\n[profiles.release]\nbackend="systemverilog"\n'
        + platform
    )


def test_profile_clock_is_strict_and_does_not_enter_implementation_request() -> None:
    manifest = _manifest(
        "[profiles.release.platform.clocks.clk]\nperiod-ns=10.000\n"
    )
    contribution = parse_selected_profile(manifest, "release")
    assert contribution.backend is not None
    profile = parse_platform_profile(manifest, "release")
    assert profile is not None
    assert profile.clocks[0].period_ns == Decimal("10.0")
    assert not hasattr(contribution, "platform")

    for platform, message in (
        ("[profiles.release.platform.clocks.clk]\nperiod-ns=0\n", "positive"),
        ("[profiles.release.platform.clocks.clk]\nperiod-ns=-1\n", "positive"),
        ("[profiles.release.platform.clocks.clk]\nperiod-ns='10'\n", "numeric"),
        ("[profiles.release.platform.clocks.clk]\nperiod-ns=10\nextra=1\n", "unknown key"),
        (
            "[profiles.release.platform.clocks.clk]\nperiod-ns=10\n"
            "[profiles.release.platform.clocks.other]\nperiod-ns=20\n",
            "exactly one",
        ),
    ):
        with pytest.raises(ImplementationRequestError, match=message):
            parse_selected_profile(_manifest(platform), "release")


def test_backend_bound_xdc_and_sdc_retain_physical_reset_contract() -> None:
    result = compile_source(SOURCE)
    backend = emit_artifact(result.ir, selected_ir_identity=result.selected_ir_identity)
    profile = parse_platform_profile(
        _manifest("[profiles.release.platform.clocks.clk]\nperiod-ns=10.000\n"),
        "release",
    )
    assert profile is not None

    xdc = build_constraint_artifact(
        result.ir, backend, profile.clocks[0], ConstraintFormat.XDC
    )
    sdc = build_constraint_artifact(
        result.ir, backend, profile.clocks[0], ConstraintFormat.SDC
    )
    assert xdc.text == "create_clock -name clk -period 10 [get_ports {clk}]\n"
    assert sdc.text == xdc.text
    assert xdc.identity != sdc.identity
    assert xdc.clock_edge == "falling"
    assert xdc.reset_mode == "asynchronous"
    assert xdc.reset_polarity == "active_low"
    assert xdc.reset_release_mode == "native"
    assert xdc.reset_release_cycles == 0
    assert xdc.power_up == "unspecified"
    assert "false_path" not in xdc.text
    restored = ConstraintArtifact.from_json(xdc.to_json())
    assert restored == xdc
    data = json.loads(xdc.to_json())
    data["text"] = data["text"].replace("-period 10", "-period 9")
    with pytest.raises(PlatformConstraintError, match="content hash"):
        ConstraintArtifact.from_json(json.dumps(data))
    malformed = json.loads(xdc.to_json())
    malformed["backend"] = 7
    with pytest.raises(PlatformConstraintError, match="values are invalid"):
        ConstraintArtifact.from_json(json.dumps(malformed))
    nonfinite = json.loads(xdc.to_json())
    nonfinite["period_ns"] = "NaN"
    with pytest.raises(PlatformConstraintError, match="finite and positive"):
        ConstraintArtifact.from_json(json.dumps(nonfinite))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("backend", "mystery", "backend.*unsupported"),
        ("clock_edge", "sideways", "clock edge.*unsupported"),
        ("reset_mode", "sometimes", "reset mode.*unsupported"),
        ("reset_polarity", "maybe", "reset polarity.*unsupported"),
        ("reset_release_mode", "eventually", "reset release mode.*unsupported"),
        ("power_up", "magic", "power-up policy.*unsupported"),
    ),
)
def test_constraint_json_rejects_rehashed_invalid_physical_metadata(
    field: str, value: str, message: str,
) -> None:
    result = compile_source(SOURCE)
    backend = emit_artifact(
        result.ir, selected_ir_identity=result.selected_ir_identity
    )
    constraint = parse_platform_profile(
        _manifest("[profiles.release.platform.clocks.clk]\nperiod-ns=10\n"),
        "release",
    ).clocks[0]
    artifact = build_constraint_artifact(
        result.ir, backend, constraint, ConstraintFormat.XDC
    )
    data = json.loads(artifact.to_json())
    data[field] = value
    data["identity"] = stable_digest({
        key: item
        for key, item in data.items()
        if key not in {"identity", "text"}
    })
    with pytest.raises(PlatformConstraintError, match=message):
        ConstraintArtifact.from_json(json.dumps(data))


def test_constraint_json_rejects_invalid_reset_release_contract() -> None:
    result = compile_source(SOURCE)
    backend = emit_artifact(
        result.ir, selected_ir_identity=result.selected_ir_identity
    )
    constraint = parse_platform_profile(
        _manifest("[profiles.release.platform.clocks.clk]\nperiod-ns=10\n"),
        "release",
    ).clocks[0]
    artifact = build_constraint_artifact(
        result.ir, backend, constraint, ConstraintFormat.XDC
    )
    data = json.loads(artifact.to_json())
    data["reset_release_mode"] = "synchronized"
    data["reset_release_cycles"] = 0
    data["identity"] = stable_digest({
        key: item
        for key, item in data.items()
        if key not in {"identity", "text"}
    })
    with pytest.raises(
        PlatformConstraintError,
        match="physical reset contract is invalid.*exactly two cycles",
    ):
        ConstraintArtifact.from_json(json.dumps(data))


def test_synchronized_release_is_published_and_bound_to_manifest() -> None:
    source = SOURCE.replace(
        "clock clk { edge falling }\n  reset rst @clk {\n"
        "    mode asynchronous\n    polarity active_low\n"
        "    power_up unspecified\n  }",
        "clock clk\n  async reset rst @clk",
    )
    result = compile_source(source)
    backend = emit_artifact(
        result.ir, selected_ir_identity=result.selected_ir_identity
    )
    constraint = parse_platform_profile(
        _manifest("[profiles.release.platform.clocks.clk]\nperiod-ns=10\n"),
        "release",
    ).clocks[0]
    artifact = build_constraint_artifact(
        result.ir, backend, constraint, ConstraintFormat.XDC
    )
    assert artifact.reset_release_mode == "synchronized"
    assert artifact.reset_release_cycles == 2
    assert ConstraintArtifact.from_json(artifact.to_json()) == artifact

    physical = backend.physical_domains[0]
    stale = replace(
        backend,
        physical_domains=(replace(
            physical,
            reset_release_mode="native",
            reset_release_cycles=0,
        ),),
    )
    with pytest.raises(
        PlatformConstraintError,
        match="physical-domain manifest does not match typed reset contract",
    ):
        build_constraint_artifact(
            result.ir, stale, constraint, ConstraintFormat.XDC
        )


def test_constraint_binding_validation_fails_closed() -> None:
    result = compile_source(SOURCE)
    backend = emit_artifact(result.ir, selected_ir_identity=result.selected_ir_identity)
    constraint = parse_platform_profile(
        _manifest("[profiles.release.platform.clocks.clk]\nperiod-ns=8\n"),
        "release",
    ).clocks[0]
    clock = next(item for item in backend.bindings if item.role is SignalRole.CLOCK)
    reset = next(item for item in backend.bindings if item.role is SignalRole.RESET)

    malformed = (
        replace(backend, bindings=tuple(item for item in backend.bindings if item is not clock)),
        replace(backend, bindings=tuple(
            replace(item, width=2) if item is clock else item
            for item in backend.bindings
        )),
        replace(backend, bindings=tuple(
            replace(item, physical_available=False) if item is reset else item
            for item in backend.bindings
        )),
        replace(backend, bindings=tuple(
            replace(item, artifact_hash="0" * 64) if item is clock else item
            for item in backend.bindings
        )),
        replace(backend, bindings=tuple(
            replace(item, selected_ir_identity="stale:selected")
            if item.role is SignalRole.OUTPUT else item
            for item in backend.bindings
        )),
    )
    for artifact in malformed:
        with pytest.raises(PlatformConstraintError):
            build_constraint_artifact(
                result.ir, artifact, constraint, ConstraintFormat.XDC
            )


def test_constraint_requires_exact_physical_domain_binding_paths() -> None:
    source = SOURCE.replace(
        "clock clk { edge falling }\n  reset rst @clk {\n"
        "    mode asynchronous\n    polarity active_low\n"
        "    power_up unspecified\n  }",
        "clock clk\n  async reset rst @clk",
    )
    result = compile_source(source)
    backend = emit_artifact(
        result.ir, selected_ir_identity=result.selected_ir_identity
    )
    constraint = parse_platform_profile(
        _manifest("[profiles.release.platform.clocks.clk]\nperiod-ns=10\n"),
        "release",
    ).clocks[0]
    physical = backend.physical_domains[0]
    stale = replace(
        backend,
        physical_domains=(replace(physical, rtl_clock_path="stale_clk"),),
    )

    with pytest.raises(
        PlatformConstraintError,
        match="physical domain clock locator does not match",
    ):
        build_constraint_artifact(
            result.ir, stale, constraint, ConstraintFormat.XDC
        )


def test_constraint_publication_is_atomic_and_rejects_symlink(tmp_path: Path) -> None:
    result = compile_source(SOURCE)
    backend = emit_artifact(result.ir, selected_ir_identity=result.selected_ir_identity)
    constraint = parse_platform_profile(
        _manifest("[profiles.release.platform.clocks.clk]\nperiod-ns=10\n"),
        "release",
    ).clocks[0]
    artifact = build_constraint_artifact(
        result.ir, backend, constraint, ConstraintFormat.XDC
    )
    output = tmp_path / "top.xdc"
    assert publish_constraint_artifact(artifact, output) == output
    assert output.read_text() == artifact.text
    output.unlink()
    target = tmp_path / "target.xdc"
    target.write_text("untouched")
    output.symlink_to(target)
    with pytest.raises(PlatformConstraintError, match="symbolic link"):
        publish_constraint_artifact(artifact, output)
    assert target.read_text() == "untouched"




def test_generated_constraint_is_valid_tcl(tmp_path: Path) -> None:
    tclsh = shutil.which("tclsh")
    if tclsh is None:
        pytest.skip("tclsh is unavailable")
    result = compile_source(SOURCE)
    backend = emit_artifact(result.ir, selected_ir_identity=result.selected_ir_identity)
    constraint = parse_platform_profile(
        _manifest("[profiles.release.platform.clocks.clk]\nperiod-ns=10\n"),
        "release",
    ).clocks[0]
    artifact = build_constraint_artifact(
        result.ir, backend, constraint, ConstraintFormat.XDC
    )
    constraint_path = tmp_path / "top.xdc"
    publish_constraint_artifact(artifact, constraint_path)
    driver = tmp_path / "read.tcl"
    driver.write_text(
        "proc get_ports {args} { return $args }\n"
        "proc create_clock {args} { return }\n"
        f"source {{{constraint_path.as_posix()}}}\n"
    )
    completed = subprocess.run(
        (tclsh, str(driver)), text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    source_dir = root / "src"
    source_dir.mkdir(parents=True)
    manifest = root / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="constraints"\nversion="1"\n'
        'source-root="src"\n[profiles.release]\nbackend="systemverilog"\n'
        '[profiles.release.platform.clocks.clk]\nperiod-ns=10\n'
    )
    source = source_dir / "top.zhl"
    source.write_text(SOURCE)
    update_project_lock(manifest)
    return manifest, source


def test_cli_publishes_backend_specific_constraints_and_manifest(tmp_path: Path) -> None:
    project_manifest, source = _project(tmp_path)
    sv = tmp_path / "top.sv"
    xdc = tmp_path / "top.xdc"
    sdc = tmp_path / "top.sdc"
    manifest = tmp_path / "build.json"
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main((
            str(source), "--profile", "release", "--systemverilog", str(sv),
            "--constraints-xdc", str(xdc), "--constraints-sdc", str(sdc),
            "--build-manifest", str(manifest),
        ))
    assert code == 0, stderr.getvalue()
    assert stdout.getvalue() == ""
    assert xdc.read_text() == sdc.read_text()
    data = json.loads(manifest.read_text())
    backend = data["backend_builds"][0]
    assert {item["kind"] for item in backend["companions"]} >= {
        "platform_constraint:xdc", "platform_constraint:sdc"
    }
    metadata = dict(data["metadata"])
    assert metadata["platform_constraint.direct_systemverilog.xdc.identity"]
    assert metadata["platform_constraint.direct_systemverilog.xdc.reset"] == (
        "asynchronous:active_low:unspecified"
    )

    second_sv = tmp_path / "top-second.sv"
    second_xdc = tmp_path / "top-second.xdc"
    second_manifest = tmp_path / "build-second.json"
    project_manifest.write_text(
        project_manifest.read_text().replace("period-ns=10", "period-ns=8")
    )
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        assert main((
            str(source), "--profile", "release", "--systemverilog", str(second_sv),
            "--constraints-xdc", str(second_xdc),
            "--build-manifest", str(second_manifest),
        )) == 0
    second_data = json.loads(second_manifest.read_text())
    assert data["profile_identity"] != second_data["profile_identity"]
    assert data["build_identity"] != second_data["build_identity"]


def test_cli_constraint_request_requires_profile_and_one_backend(tmp_path: Path) -> None:
    _, source = _project(tmp_path)
    with pytest.raises(SystemExit):
        main((str(source), "--systemverilog", str(tmp_path / "x.sv"),
              "--constraints-xdc", str(tmp_path / "x.xdc")))
    with pytest.raises(SystemExit):
        main((str(source), "--profile", "release",
              "--constraints-xdc", str(tmp_path / "x.xdc")))
