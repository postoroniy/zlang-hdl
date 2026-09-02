from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from zlang.compiler import compile_file, compile_source
from zlang.implementation_request import ImplementationRequestError
from zlang.workspace import update_project_lock


ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path, source: str, profile: str = "") -> tuple[Path, Path]:
    root = tmp_path / "profile-project"
    (root / "src").mkdir(parents=True)
    manifest = root / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="profiles"\nversion="1"\n'
        'source-root="src"\n' + profile
    )
    top = root / "src" / "top.zhl"
    top.write_text(source)
    update_project_lock(manifest)
    return manifest, top


def test_profile_requires_project_and_unknown_profile_is_structured(tmp_path: Path) -> None:
    source = tmp_path / "plain.zhl"
    source.write_text("module Plain { out y:u8 y=1 }")
    with pytest.raises(ImplementationRequestError, match="requires a zlang.toml"):
        compile_file(source, profile="release", include_clash=False)

    _, top = _project(tmp_path, source.read_text(), "[profiles.debug]\nbackend='clash'\n")
    with pytest.raises(ImplementationRequestError, match="unknown implementation profile"):
        compile_file(top, profile="missing", include_clash=False)


def test_profile_normalizes_backend_target_policy_and_reports_both_routes(
    tmp_path: Path,
) -> None:
    _, top = _project(
        tmp_path,
        "module Add { in a:u8 in b:u8 out y:u9 y=a+b }",
        """
[profiles.release]
backend = "systemverilog"
backend-mode = "required"
formal-policy = "off"
evidence-policy = "estimate_only"
""",
    )
    first = compile_file(top, profile="release", include_clash=False)
    second = compile_file(top, profile="release", include_clash=False)
    assert first.implementation_request.identity == second.implementation_request.identity
    assert first.implementation_policy.identity == second.implementation_policy.identity
    assert first.backend_implementation_plans.identity == second.backend_implementation_plans.identity
    assert first.backend_implementation_plans.plan_for("clash").status.value == "not_requested"
    assert first.backend_implementation_plans.plan_for("systemverilog").status.value == "selected"
    assert first.implementation_graph.realization_backend == "backend_independent"
    assert "backend systemverilog: selected" in first.backend_implementation_report


def test_region_selector_is_exact_and_profile_constraint_executes_m34(
    tmp_path: Path,
) -> None:
    manifest, top = _project(
        tmp_path,
        "module Add { in a:u8 in b:u8 out y:u9 y=a+b }",
    )
    discovery = compile_file(top, include_clash=False)
    region = discovery.implementation_regions[0]
    with manifest.open("a") as output:
        output.write(
            "\n[profiles.small]\n"
            f"regions=[\"{region.identity}\"]\n"
            'objective="minimize lut"\n'
            "[profiles.small.constraints]\nlatency=0\nii=1\n"
        )
    selected = compile_file(top, profile="small", include_clash=False)
    assert selected.implementation_request.regions == (region.identity,)
    assert len(selected.exploration_results) == 1
    assert selected.exploration_results[0].selected_candidate.stages == ("source",)

    manifest.write_text(manifest.read_text().replace(region.identity, "0" * 64))
    with pytest.raises(Exception, match="unknown or stale implementation region"):
        compile_file(top, profile="small", include_clash=False)


def test_profile_and_legacy_pipeline_auto_share_one_compatible_request(
    tmp_path: Path,
) -> None:
    _, top = _project(
        tmp_path,
        """
module Timed {
  clock clk
  reset rst
  in a:u8 in b:u8 in c:u8 in d:u8
  in e:u8 in f:u8 in g:u8 in h:u8
  out y : u19
  y = pipeline(auto, latency<=4, ii==1, fmax>=100) {
    a*b + c*d + e*f + g*h
  }
}
""",
        """
[profiles.compatible]
allowed-transforms = ["pipeline"]
objective = "maximize fmax"
[profiles.compatible.constraints]
latency = 4
ii = 1
fmax = 100
""",
    )
    result = compile_file(top, profile="compatible", include_clash=False)
    policy = next(item for item in result.implementation_policy.regions if item.source_form)
    assert policy.source_form == "pipeline(auto)"
    assert policy.request.transforms.allowed[0].value == "pipeline"
    # The legacy selector executes once; profile normalization never reruns it.
    assert len(result.ir.pipeline_explorations) == 1


def test_conflicting_profile_and_source_policy_names_origins(tmp_path: Path) -> None:
    _, top = _project(
        tmp_path,
        """
module Timed {
  clock clk reset rst
  in a:u8 in b:u8 in c:u8 in d:u8
  in e:u8 in f:u8 in g:u8 in h:u8 out y:u19
  y = pipeline(auto, latency<=4, ii==1, fmax>=100) {
    a*b + c*d + e*f + g*h
  }
}
""",
        """
[profiles.conflict]
allowed-transforms = ["pipeline"]
objective = "minimize lut"
""",
    )
    with pytest.raises(ImplementationRequestError) as caught:
        compile_file(top, profile="conflict", include_clash=False)
    assert "conflicting implementation policy for 'objective'" in str(caught.value)
    assert "source pipeline(auto)" in caught.value.notes[0]
    assert "profile 'conflict'" in caught.value.notes[1]


def test_exact_timing_cannot_be_weakened_by_profile(tmp_path: Path) -> None:
    _, top = _project(
        tmp_path,
        """
module Timed {
  clock clk reset rst in x:u8 out y:u8
  y = pipeline(4) { x }
  timing { latency 4 ii 1 }
}
""",
        """
[profiles.bad.constraints]
latency = 3
""",
    )
    with pytest.raises(ImplementationRequestError, match="conflicts with exact module timing"):
        compile_file(top, profile="bad", include_clash=False)


def test_cli_profile_reports_are_deterministic_and_json_error_is_structured(
    tmp_path: Path,
) -> None:
    manifest, top = _project(
        tmp_path,
        "module Add { in a:u8 in b:u8 out y:u9 y=a+b }",
        "[profiles.release]\nbackend='systemverilog'\nbackend-mode='required'\n",
    )
    policy = tmp_path / "policy.txt"
    backends = tmp_path / "backends.txt"
    command = (
        sys.executable, "-m", "zlang.cli", str(top), "--profile", "release",
        "--implementation-policy-report", str(policy),
        "--backend-implementation-report", str(backends),
    )
    completed = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert "policy identity:" in policy.read_text()
    assert "backend clash: not_requested" in backends.read_text()

    failed = subprocess.run(
        (
            sys.executable, "-m", "zlang.cli", str(top),
            "--profile", "missing", "--check", "--diagnostic-format", "json",
        ),
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert failed.returncode == 1
    diagnostic = json.loads(failed.stderr)
    assert diagnostic["code"] == "ZL-IMPL-001"
    assert "unknown implementation profile" in diagnostic["message"]
