# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bootstrap command contracts without network or host editor mutations."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("make")


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    (root / "Makefile").write_text(
        f"include {ROOT / 'Makefile'}\n"
        "env-check native-release-set native-release-install editor-test editor-install:\n"
        "\t@\"$(PYTHON)\" -c 'import sys' $@\n"
    )
    (root / "tools").mkdir()
    for name in ("bootstrap_local_env.sh", "run_local_env.sh"):
        source = ROOT / "tools" / name
        if source.exists():
            shutil.copyfile(source, root / "tools" / name)
    bin_dir = root / "host-bin"
    bin_dir.mkdir()
    for name in ("make", "mkdir", "mktemp", "rm", "dirname", "find", "sort"):
        (bin_dir / name).symlink_to(shutil.which(name))
    shim = root / "shim.py"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, shutil, sys\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['BOOTSTRAP_TEST_LOG'], 'a') as output:\n"
        "    output.write(json.dumps({'name': name, 'args': args, 'tmp': os.getenv('TMPDIR'), "
        "'tmp_alias': os.getenv('TMP'), 'temp': os.getenv('TEMP'), "
        "'venv': os.getenv('VIRTUAL_ENV')}) + '\\n')\n"
        "if os.getenv('BOOTSTRAP_TEST_FORBID_EXECUTION'):\n"
        "    sys.exit(99)\n"
        "def create(path):\n"
        "    target = pathlib.Path(path) / 'bin' / 'python'\n"
        "    target.parent.mkdir(parents=True, exist_ok=True)\n"
        "    shutil.copyfile(os.environ['BOOTSTRAP_TEST_SHIM'], target)\n"
        "    target.chmod(0o755)\n"
        "if name == 'uv' and args[0] == 'venv':\n"
        "    create(args[-1])\n"
        "elif name.startswith('python'):\n"
        "    if args == ['-m', 'pip', '--version'] and os.getenv('BOOTSTRAP_TEST_MISSING_PIP'):\n"
        "        sys.exit(1)\n"
        "    if args[:2] == ['-m', 'ensurepip'] and os.getenv('BOOTSTRAP_TEST_MISSING_PIP') == 'unavailable':\n"
        "        sys.exit(1)\n"
        "    if args[:2] == ['-m', 'venv']:\n"
        "        create(args[-1])\n"
        "    elif args[:1] == ['-c']:\n"
        "        if args[-1] == os.getenv('BOOTSTRAP_TEST_FAIL_TARGET'):\n"
        "            sys.exit(1)\n"
        "        if 'sys.version_info' in args[1]:\n"
        "            if os.getenv('BOOTSTRAP_TEST_BAD_PYTHON') == name:\n"
        "                sys.exit('CPython 3.12 required; found 3.13')\n"
        "        elif 'zlang._version' in args[1]:\n"
        "            print('0.1.0a24')\n"
        "elif name == 'node':\n"
        "    print(os.getenv('BOOTSTRAP_TEST_NODE_MAJOR', '22'))\n"
        "elif name == 'uname':\n"
        "    print('Linux' if args == ['-s'] else 'x86_64')\n"
    )
    shim.chmod(0o755)
    for name in ("uv", "python3.12", "python3", "node", "npm", "code", "uname"):
        (bin_dir / name).symlink_to(shim)
    # Do not inherit credentials, Make overrides, active venvs or BASH_ENV;
    # fixture failures may be reported in public CI logs.
    environment = {
        "PATH": str(bin_dir),
        "PYTHONNOUSERSITE": "1",
        "BOOTSTRAP_TEST_LOG": str(root / "calls.jsonl"),
        "BOOTSTRAP_TEST_SHIM": str(shim),
    }
    return root, environment


def _calls(root):
    log = root / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _make(checkout, *arguments, **environment):
    root, original = checkout
    return subprocess.run(
        (MAKE, *arguments), cwd=root, env=original | environment,
        capture_output=True, text=True, timeout=20,
    )


@pytest.mark.parametrize("flags", (("-n",), ("-n", "-j2"), ("--just-print",)))
@pytest.mark.parametrize("target", ("venv", "install"))
def test_make_venv_dry_run_has_no_side_effects(checkout, flags, target):
    root, _ = checkout
    before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    result = _make(checkout, *flags, target, BOOTSTRAP_TEST_FORBID_EXECUTION="1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    assert not _calls(root)
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == before


@pytest.mark.parametrize("use_uv", (False, True))
@pytest.mark.parametrize("editor_opt_out", (None, "command-line", "environment"))
def test_make_install_is_runtime_setup_with_optional_editor(checkout, use_uv, editor_opt_out):
    root, _ = checkout
    if not use_uv:
        (root / "host-bin/uv").unlink()
    arguments = ["-s", "install"]
    environment = {}
    if editor_opt_out == "command-line":
        arguments.append("INSTALL_EDITOR=0")
    elif editor_opt_out == "environment":
        environment["INSTALL_EDITOR"] = "0"
    result = _make(checkout, *arguments, **environment)
    assert result.returncode == 0, result.stderr
    calls = _calls(root)
    installs = [call["args"] for call in calls if "install" in call["args"]]
    assert len(installs) == 2
    editable = next(args for args in installs if "-e" in args)
    assert editable[editable.index("-e") + 1] == "."
    assert not any(arg.startswith(("pytest", "ruff", "reuse", "twine"))
                   for args in installs for arg in args)
    assert any(call["args"][-1:] == ["native-release-install"] for call in calls)
    assert any(call["args"][-1:] == ["editor-install"] for call in calls) == (editor_opt_out is None)


@pytest.mark.parametrize("use_uv", (False, True))
def test_make_venv_installs_local_compiler_native_and_editor(checkout, use_uv):
    root, _ = checkout
    if not use_uv:
        (root / "host-bin/uv").unlink()
    result = _make(checkout, "-s", "venv")
    assert result.returncode == 0, result.stderr
    calls = _calls(root)
    installs = [call for call in calls if "install" in call["args"]]
    assert len(installs) == 3
    editable = next(call for call in installs if ".[test]" in call["args"])
    assert "--no-build-isolation" in editable["args"]
    release_tools = next(call for call in installs if "reuse==5.1.1" in call["args"])
    assert "--no-build-isolation" not in release_tools["args"]
    assert "ruff==0.12.12" in release_tools["args"]
    quarantine = next(call for call in calls if "--quarantine-stale-metadata" in call["args"])
    assert calls.index(quarantine) < calls.index(installs[0])
    assert any(call["args"][-1:] == ["native-release-install"] for call in calls)
    assert any(call["args"][-1:] == ["editor-install"] for call in calls)
    if use_uv:
        assert next(call for call in calls if call["name"] == "uv")["args"][:3] == [
            "venv", "--python", "3.12",
        ]
        assert all(str(root / ".venv/bin/python") in call["args"] for call in installs)
    for call in installs:
        assert call["tmp"] == call["tmp_alias"] == call["temp"]
        scratch = Path(call["tmp"])
        assert scratch.parent == root / "build/tmp"
        assert not scratch.exists()


@pytest.mark.parametrize("use_uv", (False, True))
@pytest.mark.parametrize("profile", ("runtime", "dev", "release"))
def test_python_profiles_select_dependencies_and_keep_native_audit(checkout, use_uv, profile):
    root, _ = checkout
    if not use_uv:
        (root / "host-bin/uv").unlink()
    result = _make(checkout, "-s", "venv", f"PYTHON_PROFILE={profile}")
    assert result.returncode == 0, result.stderr
    calls = _calls(root)
    installs = [call["args"] for call in calls if "install" in call["args"]]
    editable = next(args for args in installs if "-e" in args)
    assert editable[editable.index("-e") + 1] == ("." if profile == "runtime" else ".[test]")
    assert "--no-build-isolation" in editable
    assert len(installs) == (2 if profile == "runtime" else 3)
    tools = [arg for args in installs for arg in args if "==" in arg]
    assert ("ruff==0.12.12" in tools) == (profile != "runtime")
    for pin in ("cyclonedx-bom==7.0.0", "pip-audit==2.10.1", "reuse==5.1.1",
                "twine==6.2.0", "wheel==0.46.3"):
        assert (pin in tools) == (profile == "release")
    assert any(call["args"][-1:] == ["native-release-install"] for call in calls)
    assert any(call["args"][-1:] == ["editor-install"] for call in calls) == (profile == "release")


@pytest.mark.parametrize("profile", ("", "DEV", "unknown", "dev release"))
def test_unknown_python_profile_fails_before_provisioning(checkout, profile):
    root, _ = checkout
    result = _make(checkout, "-s", "venv", f"PYTHON_PROFILE={profile}")
    assert result.returncode != 0
    assert "PYTHON_PROFILE must be runtime, dev or release" in result.stderr
    assert not _calls(root)
    assert not (root / ".venv").exists()
    assert not (root / "build").exists()


@pytest.mark.parametrize("use_uv", (False, True))
def test_runtime_profile_does_not_prune_existing_external_packages(checkout, use_uv):
    root, environment = checkout
    if not use_uv:
        (root / "host-bin/uv").unlink()
    interpreter = root / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    shutil.copyfile(environment["BOOTSTRAP_TEST_SHIM"], interpreter)
    interpreter.chmod(0o755)
    marker = root / ".venv/lib/python3.12/site-packages/external-package.marker"
    marker.parent.mkdir(parents=True)
    marker.write_text("owned by another workflow\n")
    result = _make(checkout, "-s", "venv", "PYTHON_PROFILE=runtime")
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "owned by another workflow\n"
    assert not any(set(call["args"]) & {"sync", "uninstall"} for call in _calls(root))


@pytest.mark.parametrize("profile", ("runtime", "dev"))
def test_smaller_profiles_honor_explicit_editor_install(checkout, profile):
    root, _ = checkout
    result = _make(checkout, "-s", "venv", f"PYTHON_PROFILE={profile}", "INSTALL_EDITOR=1")
    assert result.returncode == 0, result.stderr
    assert any(call["args"][-1:] == ["editor-install"] for call in _calls(root))


def test_runtime_compilation_and_native_execution_do_not_import_dev_tools():
    script = '''
import importlib.abc
import sys

blocked = {"pytest", "xdist", "z3", "yaml", "build", "ruff", "pip_audit",
           "reuse", "twine", "cyclonedx", "cyclonedx_py"}

class RuntimeOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ModuleNotFoundError("runtime profile must not import " + fullname)

sys.meta_path.insert(0, RuntimeOnly())
from zlang.compiler import compile_source
from zlang.native_simulation import simulate

compiled = compile_source("module RuntimeProbe { in a,b:u8 out y:u9 y=a+b }")
assert simulate(compiled.ir, a=7, b=9) == {"y": 16}
print("runtime-only compile/native execution passed")
'''
    result = subprocess.run(
        (sys.executable, "-c", script), cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime-only compile/native execution passed" in result.stdout


@pytest.mark.parametrize("unavailable", ("node", "npm", "old-node", "code"))
def test_optional_editor_prerequisites_do_not_break_python_bootstrap(checkout, unavailable):
    root, _ = checkout
    environment = {}
    if unavailable == "old-node":
        environment["BOOTSTRAP_TEST_NODE_MAJOR"] = "20"
    else:
        (root / "host-bin" / unavailable).unlink()
    result = _make(checkout, "-s", "venv", **environment)
    assert result.returncode == 0, result.stderr
    assert any(call["args"][-1:] == ["native-release-install"] for call in _calls(root))
    assert not any(call["args"][-1:] == ["editor-install"] for call in _calls(root))
    assert "make editor-install" in result.stdout + result.stderr


@pytest.mark.parametrize("option", ("INSTALL_EDITOR=0", "CI=1"))
def test_explicit_or_ci_editor_opt_out_is_respected(checkout, option):
    root, _ = checkout
    result = _make(checkout, "-s", "venv", option)
    assert result.returncode == 0, result.stderr
    assert not any(call["args"][-1:] == ["editor-install"] for call in _calls(root))


@pytest.mark.parametrize("foreign", (
    "outside", "symlink", "nested-symlink", "dangling-symlink", "active", "tmp",
))
def test_bootstrap_rejects_foreign_environment_before_installing(checkout, foreign):
    root, _ = checkout
    sibling = root.parent / "foreign"
    sibling.mkdir()
    arguments = []
    environment = {}
    if foreign == "outside":
        arguments.append(f"VENV={sibling}")
    elif foreign == "symlink":
        (root / ".venv").symlink_to(sibling, target_is_directory=True)
    elif foreign == "nested-symlink":
        (root / "environments").symlink_to(sibling, target_is_directory=True)
        arguments.append(f"VENV={root / 'environments/new'}")
    elif foreign == "dangling-symlink":
        (root / ".venv").symlink_to(sibling / "missing", target_is_directory=True)
    elif foreign == "tmp":
        arguments.append(f"TMP_ROOT={sibling}")
    else:
        environment["VIRTUAL_ENV"] = str(sibling)
    result = _make(checkout, "-s", "venv", *arguments, **environment)
    assert result.returncode != 0
    assert "worktree" in result.stderr or "not a usable directory" in result.stderr
    assert not _calls(root)
    assert not tuple(sibling.iterdir())


def test_existing_wrong_python_fails_before_any_package_install(checkout):
    root, environment = checkout
    interpreter = root / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    shutil.copyfile(environment["BOOTSTRAP_TEST_SHIM"], interpreter)
    interpreter.chmod(0o755)
    result = _make(checkout, "-s", "venv", BOOTSTRAP_TEST_BAD_PYTHON="python")
    assert result.returncode != 0
    assert "CPython 3.12" in result.stderr
    assert not any("install" in call["args"] for call in _calls(root))


def test_bootstrap_reports_missing_python_without_uv(checkout):
    root, _ = checkout
    for name in ("uv", "python3", "python3.12"):
        (root / "host-bin" / name).unlink()
    result = _make(checkout, "-s", "venv")
    assert result.returncode != 0
    assert "CPython 3.12" in result.stderr and "uv" in result.stderr
    assert all(call["name"] == "uname" for call in _calls(root))


def test_bootstrap_falls_back_to_compatible_python3(checkout):
    root, _ = checkout
    (root / "host-bin/uv").unlink()
    result = _make(checkout, "-s", "venv", BOOTSTRAP_TEST_BAD_PYTHON="python3.12")
    assert result.returncode == 0, result.stderr
    creator = next(call for call in _calls(root) if call["args"][:2] == ["-m", "venv"])
    assert creator["name"] == "python3"


@pytest.mark.parametrize("ensurepip", ("available", "unavailable"))
def test_pip_bootstrap_recovers_or_reports_a_missing_pip(checkout, ensurepip):
    root, _ = checkout
    (root / "host-bin/uv").unlink()
    result = _make(checkout, "-s", "venv", BOOTSTRAP_TEST_MISSING_PIP=ensurepip)
    calls = _calls(root)
    assert any(call["args"] == ["-m", "ensurepip", "--upgrade"] for call in calls)
    if ensurepip == "available":
        assert result.returncode == 0, result.stderr
        assert any("install" in call["args"] for call in calls)
    else:
        assert result.returncode != 0
        assert "pip is unavailable" in result.stderr
        assert not any("install" in call["args"] for call in calls)


@pytest.mark.parametrize("target", ("native-release-install", "editor-install"))
def test_bootstrap_reports_failed_install_stage_without_claiming_full_success(checkout, target):
    root, _ = checkout
    result = _make(checkout, "-s", "venv", BOOTSTRAP_TEST_FAIL_TARGET=target)
    assert result.returncode != 0
    assert "installation failed" in result.stderr
    if target == "editor-install":
        assert "compiler/native/LSP installation succeeded" in result.stderr
    else:
        assert "Compiler, native runtime and zlang-lsp installed" not in result.stdout
        assert not any(call["args"][-1:] == ["editor-install"] for call in _calls(root))
