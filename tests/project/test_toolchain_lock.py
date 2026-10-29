# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Opt-in SDK locks never substitute version strings or warm cache evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from zlang.compiler import compile_file
from zlang.incremental_workspace import IncrementalWorkspaceSession
from zlang.project import ProjectManifest, ProjectLock
from zlang.toolchain_lock import LockedToolchain
from zlang.workspace import WorkspaceError, load_project_workspace, update_project_lock


def sdk(digest="1" * 64):
    files = (("compiler.py", digest),)
    runtime = (("python-abi", "cp312"),)
    modules = (("std.bus.reg", "2" * 64),)
    return LockedToolchain(
        LockedToolchain.identify("0.1.0a24", files, runtime, modules),
        files,
        runtime,
        modules,
    )


def project(root, name="demo", requirement=None, dependency=""):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "src/top.zhl"
    source.parent.mkdir(exist_ok=True)
    source.write_text("module Top {in a:bit out y:bit y=a}")
    requirement = requirement or sdk().requirement
    text = f'schema=2\n[project]\nname="{name}"\nversion="0.1"\nsource-root="src"\n'
    if dependency:
        text += "[dependencies]\n" + dependency + "\n"
    text += "[toolchain]\n" + "".join(
        f'{k}="{v}"\n' for k, v in requirement.to_data().items()
    )
    (root / "zlang.toml").write_text(text)
    return source


@pytest.fixture
def installed(monkeypatch):
    value = sdk()
    monkeypatch.setattr("zlang.toolchain_lock.current_toolchain", lambda: value)
    return value


def test_exact_models_round_trip_and_legacy_bytes_stay_legacy(tmp_path, installed):
    source = project(tmp_path)
    manifest = ProjectManifest.load(tmp_path / "zlang.toml")
    assert ProjectManifest.parse(manifest.render()).toolchain == installed.requirement
    assert ProjectManifest.from_data(manifest.to_data()).to_data() == manifest.to_data()
    lock = update_project_lock(tmp_path)
    assert lock.schema == 3 and lock.toolchain == installed
    assert ProjectLock.parse(lock.render()) == lock
    assert ProjectLock.from_data(lock.to_data()) == lock
    assert "/home/" not in lock.render()
    assert load_project_workspace(source).lock == lock
    legacy = ProjectLock(2, "0" * 64)
    assert (
        legacy.render()
        == 'schema = 2\nmanifest-resolution-digest = "' + "0" * 64 + '"\n'
    )
    assert "toolchain" not in legacy.to_data()


@pytest.mark.parametrize("mutation", ("compiler", "stdlib", "runtime", "native_abi"))
def test_changed_sdk_rejects_load_compile_update_and_cached_products(
    tmp_path, installed, monkeypatch, mutation
):
    source = project(tmp_path)
    update_project_lock(tmp_path)
    cache = IncrementalWorkspaceSession()
    cache.file_snapshot(source, source.read_text())
    previous = (tmp_path / "zlang.lock").read_bytes()
    if mutation == "compiler":
        changed = sdk("3" * 64)
    else:
        files, runtime, modules = (
            installed.compiler_files,
            installed.runtime,
            installed.stdlib_modules,
        )
        if mutation == "stdlib":
            modules = (("std.bus.reg", "4" * 64),)
        elif mutation == "runtime":
            runtime = (("python-abi", "cp313"),)
        else:
            runtime = (*runtime, ("native-abi", "14"))
        changed = LockedToolchain(
            LockedToolchain.identify("0.1.0a24", files, runtime, modules),
            files,
            runtime,
            modules,
        )
    monkeypatch.setattr("zlang.toolchain_lock.current_toolchain", lambda: changed)
    for action in (
        lambda: load_project_workspace(source),
        lambda: compile_file(source),
        lambda: cache.file_snapshot(source, source.read_text()),
        lambda: update_project_lock(tmp_path),
    ):
        with pytest.raises(WorkspaceError, match="toolchain"):
            action()
    assert (tmp_path / "zlang.lock").read_bytes() == previous


def test_warm_tooling_and_trivia_cannot_hide_sdk_mutation(
    tmp_path, installed, monkeypatch
):
    from zlang.tooling_session import ToolingSession

    source = project(tmp_path)
    update_project_lock(tmp_path)
    session = ToolingSession()
    session.semantic_snapshot(source, source.read_text())
    monkeypatch.setattr("zlang.toolchain_lock.current_toolchain", lambda: sdk("9" * 64))
    assert not session.trivia_diagnostic_proof(source, source.read_text() + "\n")
    with pytest.raises(WorkspaceError, match="toolchain"):
        session.semantic_snapshot(source, source.read_text())


def test_failed_incremental_refresh_never_blesses_an_unavailable_sdk(
    tmp_path, installed, monkeypatch
):
    source = project(tmp_path)
    update_project_lock(tmp_path)
    cache = IncrementalWorkspaceSession()
    retained = cache.file_snapshot(source, source.read_text())
    monkeypatch.setattr("zlang.toolchain_lock.current_toolchain", lambda: sdk("8" * 64))
    with pytest.raises(WorkspaceError, match="toolchain"):
        cache.refresh(retained)
    for _ in range(2):
        with pytest.raises(WorkspaceError, match="toolchain"):
            cache.file_snapshot(source, source.read_text())


def test_inventory_order_is_canonical():
    files = (("z.py", "1" * 64), ("a.py", "2" * 64))
    runtime = (("z", "1"), ("a", "2"))
    modules = (("std.z", "3" * 64), ("std.a", "4" * 64))
    left = LockedToolchain.identify("v", files, runtime, modules)
    right = LockedToolchain.identify("v", files[::-1], runtime[::-1], modules[::-1])
    assert left == right
    assert LockedToolchain(left, files, runtime, modules).requirement == right


def test_dependency_requirement_and_legacy_root_fail_closed(tmp_path, installed):
    dependency = tmp_path / "library"
    project(dependency, "library")
    root = tmp_path / "app"
    source = project(root, dependency='library={path="../library"}')
    update_project_lock(root)
    assert load_project_workspace(source).lock.toolchain == installed
    (dependency / "zlang.toml").write_text(
        (dependency / "zlang.toml")
        .read_text()
        .replace(installed.requirement.compiler_identity, "0" * 64)
    )
    with pytest.raises(WorkspaceError, match="toolchain"):
        update_project_lock(root)
    (root / "zlang.toml").write_text(
        'schema=1\n[project]\nname="app"\nversion="0.1"\nsource-root="src"\n[dependencies]\nlibrary={path="../library"}\n'
    )
    with pytest.raises(WorkspaceError, match="root project schema 2"):
        update_project_lock(root)


@pytest.mark.parametrize("mutation", ("digest", "missing", "schema", "path", "runtime"))
def test_malformed_toolchain_inventory_rejected(mutation):
    data = sdk().to_data()
    if mutation == "digest":
        data["compiler-identity"] = "a" * 64
    elif mutation == "missing":
        del data["stdlib-modules"]
    elif mutation == "schema":
        data["schema"] = "future"
    elif mutation == "path":
        data["compiler-files"] = {"../compiler.py": "1" * 64}
    else:
        data["runtime"] = []
    with pytest.raises(ValueError):
        LockedToolchain.from_data(data)


def test_inventory_affects_dependency_identity_and_missing_lock_rejects(
    tmp_path, installed
):
    source = project(tmp_path)
    with pytest.raises(WorkspaceError, match="unavailable"):
        load_project_workspace(source)
    lock = update_project_lock(tmp_path)
    other = replace(lock, toolchain=sdk("7" * 64))
    assert lock.identity != other.identity
    bad = replace(lock, toolchain=None, schema=2)
    (tmp_path / "zlang.lock").write_text(bad.render())
    with pytest.raises(WorkspaceError, match="schema 3"):
        load_project_workspace(source)


def test_live_compiler_replacement_requires_restart(monkeypatch):
    from zlang import toolchain_lock as owner

    monkeypatch.setattr(owner, "_compiler_files", lambda: (("new.py", "a" * 64),))
    with pytest.raises(ValueError, match="restart"):
        owner.current_toolchain()


def test_real_fingerprint_is_deterministic_and_has_no_paths():
    from zlang.toolchain_lock import current_toolchain

    first = current_toolchain()
    assert first == current_toolchain()
    assert all(not Path(name).is_absolute() for name, _ in first.compiler_files)
    assert "stdlib" not in first.requirement.to_data()
