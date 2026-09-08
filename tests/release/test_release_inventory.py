from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

import tools.release_inventory as inventory


# Exact hosted-build inventory from the cancelled 0.1.0a1 release, before any
# pip replacement. Keep this regression self-contained and independent of PyPI.
ALPHA1_REQUIREMENTS = """zlang-hdl==0.1.0a1
anywidget==0.11.0
asttokens==3.0.2
black==26.5.1
click==8.5.0
cloudpickle==3.1.2
comm==0.2.3
egglog==13.2.0
executing==2.2.1
graphviz==0.21
ipython==9.17.1
ipython_pygments_lexers==1.1.1
ipywidgets==8.1.9
jedi==0.20.0
jupyterlab_widgets==3.0.17
lark==1.3.1
matplotlib-inline==0.2.2
mypy_extensions==1.1.0
opentelemetry-api==1.44.0
packaging==26.3
parso==0.8.7
pathspec==1.1.1
pexpect==4.9.0
pip==25.0.1
platformdirs==4.11.7
prompt_toolkit==3.0.53
psutil==7.2.2
psygnal==0.15.1
ptyprocess==0.7.0
pure_eval==0.2.3
Pygments==2.21.0
pytokens==0.4.1
stack-data==0.6.3
traitlets==5.16.1
typing_extensions==4.16.0
wcwidth==0.8.3
widgetsnbextension==4.0.16
"""
SMALL = "zlang-hdl==0.1.0a2\npip==26.2.1\nFoo_Bar==1.2.0\n"


def _requirements(tmp_path: Path, text: str = SMALL) -> Path:
    path = tmp_path / "release-requirements.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _clean(expected: dict[str, str] | None = None) -> dict:
    expected = expected or {"pip": "26.2.1", "foo-bar": "1.2.0"}
    return {"dependencies": [
        {"name": name, "version": version, "vulns": []}
        for name, version in expected.items()
    ], "fixes": []}


def _runner(monkeypatch: pytest.MonkeyPatch, payload: object, code: int = 0) -> list:
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((command, kwargs))
        if payload is not None:
            path = Path(command[command.index("--output") + 1])
            path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return subprocess.CompletedProcess(command, code, "", "")

    monkeypatch.setattr(inventory.subprocess, "run", run)
    return calls


def test_cancelled_alpha1_inventory_blocks_on_installer_findings(tmp_path, monkeypatch):
    requirements = _requirements(tmp_path, ALPHA1_REQUIREMENTS)
    expected = inventory.read_inventory(requirements, "0.1.0a1")
    assert len(expected) == 36 and expected["pip"] == "25.0.1"
    result = _clean(expected)
    pip = next(row for row in result["dependencies"] if row["name"] == "pip")
    pip["vulns"] = [{"id": "PYSEC-2026-1795", "fix_versions": ["25.3"]}]
    _runner(monkeypatch, result, code=1)
    with pytest.raises(inventory.InventoryError, match="vulnerability findings"):
        inventory.audit(requirements, "0.1.0a1", tmp_path / "audit.json")
    derived = (tmp_path / "audit.requirements.txt").read_text()
    assert "pip==25.0.1\n" in derived and "zlang-hdl" not in derived
    assert requirements.read_text() == ALPHA1_REQUIREMENTS


def test_patched_complete_inventory_passes_strict_command_and_preserves_report(tmp_path, monkeypatch):
    text = ALPHA1_REQUIREMENTS.replace("0.1.0a1", "0.1.0a2").replace("pip==25.0.1", "pip==26.2.1")
    requirements = _requirements(tmp_path, text)
    result = _clean(inventory.read_inventory(requirements, "0.1.0a2"))
    calls = _runner(monkeypatch, result)
    monkeypatch.setenv("PIP_AUDIT_VULNERABILITY_SERVICE", "osv")
    report = tmp_path / "audit.json"
    assert inventory.audit(requirements, "0.1.0a2", report) == 36
    assert json.loads(report.read_text()) == result
    command, options = calls[0]
    assert command == [
        sys.executable, "-m", "pip_audit", "--strict", "--no-deps", "--disable-pip",
        "--vulnerability-service", "pypi", "--progress-spinner", "off",
        "-r", str(tmp_path / "audit.requirements.txt"), "--format", "json", "--output", str(report),
    ]
    assert options["timeout"] == 300 and options["check"] is False
    assert not any(key.startswith("PIP_AUDIT_") for key in options["env"])
    assert requirements.read_text() == text


def test_inventory_rejects_nonfrozen_or_duplicate_dependencies(tmp_path):
    invalid = [
        "pip>=26.2.1", "pip===26.2.1", "pip==26.*", "pip==v26.2.1",
        "pip==26.2.1; python_version >= '3.12'", "pip[extra]==26.2.1",
        "pip @ https://example.invalid/pip.whl", "--index-url https://example.invalid",
        "-r other.txt", "# ignored package", "pip==26.2.1 # comment",
        "pip==26.2.1\npip==26.2.1", "Foo.Bar==1.2.0", "-e ./project", "pip==NaN",
    ]
    for line in invalid:
        requirements = _requirements(tmp_path, SMALL + line + "\n")
        with pytest.raises(inventory.InventoryError):
            inventory.read_inventory(requirements, "0.1.0a2")


def test_only_one_exact_current_project_is_excluded_and_pip_is_required(tmp_path):
    invalid = [
        SMALL.replace("zlang-hdl==0.1.0a2\n", ""),
        SMALL.replace("0.1.0a2", "0.1.0a1"),
        SMALL + "zlang_hdl==0.1.0a2\n",
        SMALL.replace("pip==26.2.1\n", ""),
    ]
    for text in invalid:
        with pytest.raises(inventory.InventoryError):
            inventory.read_inventory(_requirements(tmp_path, text), "0.1.0a2")
    requirements = _requirements(tmp_path, SMALL.replace("zlang-hdl", "ZLang_HDL") + "zlang-helper==2.0\n")
    assert inventory.read_inventory(requirements, "0.1.0a2") == {
        "pip": "26.2.1", "foo-bar": "1.2.0", "zlang-helper": "2.0",
    }
    for version in ("", "0.1.*", "0.1.0a2; marker", "https://example.invalid"):
        with pytest.raises(inventory.InventoryError, match="project version"):
            inventory.read_inventory(requirements, version)


def test_report_fails_closed_on_incomplete_skipped_extra_or_malformed_rows(tmp_path):
    expected = {"pip": "26.2.1", "foo-bar": "1.2.0"}
    malformed = [
        {}, [], True, {"dependencies": [], "fixes": []},
        {"dependencies": True, "fixes": []}, {"dependencies": [], "fixes": [True]},
    ]
    for field, value in (("status", "success"), ("status", True), ("skipped", [])):
        malformed.append({**_clean(), field: value})
    for mutation in (
        {"name": "pip", "skip_reason": "unavailable"},
        {"name": "pip", "version": "26.2.1", "vulns": [], "skip_reason": "ignored"},
        {"name": "pip", "version": "26.2.1", "vulns": False},
        {"name": "pip", "version": True, "vulns": []},
        {"name": False, "version": "26.2.1", "vulns": []},
        {"name": "pip", "version": "26.2.1.0", "vulns": []},
        {"name": "extra", "version": "26.2.1", "vulns": []},
        {"name": "pip", "version": "26.2.1"}, False,
    ):
        report = _clean()
        report["dependencies"][0] = mutation
        malformed.append(report)
    duplicate = _clean()
    duplicate["dependencies"].append({"name": "Foo_Bar", "version": "1.2.0", "vulns": []})
    malformed.append(duplicate)
    path = tmp_path / "result.json"
    for report in malformed:
        path.write_text(json.dumps(report))
        with pytest.raises(inventory.InventoryError):
            inventory.check_report(path, expected)


def test_invalid_duplicate_or_nonstandard_json_fails(tmp_path):
    path = tmp_path / "result.json"
    for text in ("", "{broken", '{"dependencies":[],"dependencies":[],"fixes":[]}',
                 '{"dependencies":NaN,"fixes":[]}', '{"dependencies":Infinity,"fixes":[]}'):
        path.write_text(text)
        with pytest.raises(inventory.InventoryError):
            inventory.check_report(path, {"pip": "26.2.1"})
    with pytest.raises(inventory.InventoryError, match="missing"):
        inventory.check_report(tmp_path / "missing.json", {"pip": "26.2.1"})


def test_tool_errors_and_zero_exit_without_report_fail_closed(tmp_path, monkeypatch):
    requirements = _requirements(tmp_path)
    for code in (1, 2, -15):
        _runner(monkeypatch, _clean(), code=code)
        with pytest.raises(inventory.InventoryError, match="exit status"):
            inventory.audit(requirements, "0.1.0a2", tmp_path / f"exit-{code}.json")
    _runner(monkeypatch, None)
    with pytest.raises(inventory.InventoryError, match="invalid JSON"):
        inventory.audit(requirements, "0.1.0a2", tmp_path / "no-report.json")
    result = _clean()
    result["dependencies"][0]["vulns"] = [{"id": "finding"}]
    _runner(monkeypatch, result)
    with pytest.raises(inventory.InventoryError, match="vulnerability findings"):
        inventory.audit(requirements, "0.1.0a2", tmp_path / "exit-zero-findings.json")


def test_timeout_and_execution_failure_are_explicit(tmp_path, monkeypatch):
    requirements = _requirements(tmp_path)
    for number, error in enumerate((subprocess.TimeoutExpired("pip-audit", 300), OSError("unavailable"))):
        def run(*args, **kwargs):
            raise error
        monkeypatch.setattr(inventory.subprocess, "run", run)
        with pytest.raises(inventory.InventoryError, match="timed out|could not be executed"):
            inventory.audit(requirements, "0.1.0a2", tmp_path / f"failure-{number}.json")


def test_stale_output_and_path_aliases_are_never_overwritten(tmp_path, monkeypatch):
    requirements = _requirements(tmp_path)
    calls = _runner(monkeypatch, _clean())
    report = tmp_path / "audit.json"
    report.write_text(json.dumps(_clean()))
    with pytest.raises(inventory.InventoryError, match="fresh"):
        inventory.audit(requirements, "0.1.0a2", report)
    assert json.loads(report.read_text()) == _clean()
    with pytest.raises(inventory.InventoryError, match="distinct"):
        inventory.audit(requirements, "0.1.0a2", requirements)
    derived = tmp_path / "derived.requirements.txt"
    derived.write_text(SMALL)
    with pytest.raises(inventory.InventoryError, match="distinct"):
        inventory.audit(derived, "0.1.0a2", tmp_path / "derived.json")
    symlink = tmp_path / "alias.json"
    symlink.symlink_to(requirements)
    with pytest.raises(inventory.InventoryError, match="distinct"):
        inventory.audit(requirements, "0.1.0a2", symlink)
    assert requirements.read_text() == SMALL and calls == []


def test_cli_contract_and_failure_diagnostics(tmp_path, monkeypatch, capsys):
    requirements = _requirements(tmp_path)
    _runner(monkeypatch, _clean())
    args = ["--requirements", str(requirements), "--project-version", "0.1.0a2",
            "--report", str(tmp_path / "audit.json")]
    assert inventory.main(args) == 0
    assert "2 exact dependencies including pip" in capsys.readouterr().out
    assert inventory.main(args) == 1
    assert "outputs must be fresh" in capsys.readouterr().err
