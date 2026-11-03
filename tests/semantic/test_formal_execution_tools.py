# SPDX-License-Identifier: Apache-2.0
"""Solver selection survives OSS launcher PATH rewrites and binds result caches."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from zlang.formal_execution_tools import FormalToolBinding
from zlang.formal_exploration import FormalExplorationConfig, proof_cache_key
from zlang.ir.formal import FormalError
from zlang.verification_bundle_execution import verification_result_cache_key
from zlang.verification_bundle_report import VerificationRunConfig


def _script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)


@pytest.fixture
def wrapped_tools(tmp_path, monkeypatch):
    suite = tmp_path / 'OSS suite with spaces'
    for name in ('sby', 'yosys-smtbmc'):
        _script(suite / 'bin' / name,
                'release_bindir_abs="$(cd "$(dirname "$0")" && pwd)"\n'
                'export PATH="$release_bindir_abs:$PATH"\n'
                f'exec /bin/sh "$release_bindir_abs/../libexec/{name}" "$@"')
    _script(suite / 'libexec/sby', 'exec /bin/sh -c "${SMTBMC:-yosys-smtbmc}"')
    _script(suite / 'libexec/yosys-smtbmc', 'exec z3')
    _script(suite / 'bin/tabbypy3', 'exec "$@"')
    _script(suite / 'bin/yosys', 'echo yosys')
    _script(suite / 'bin/z3', 'echo bundled-old')
    selected = tmp_path / 'selected/bin/z3'
    _script(selected, 'echo selected')
    monkeypatch.setenv('PATH', os.pathsep.join((str(selected.parent), str(suite / 'bin'), '/usr/bin', '/bin')))
    monkeypatch.delenv('SMTBMC', raising=False)
    return suite, selected


def test_oss_child_uses_exact_selected_solver_without_changing_suite(wrapped_tools):
    suite, selected = wrapped_tools
    old = subprocess.check_output((str(suite / 'bin/sby'),), text=True).strip()
    assert old == 'bundled-old'  # The caller PATH alone demonstrably fails.
    before = {p: p.read_bytes() for p in (suite / 'bin').iterdir()}
    environment_before = dict(os.environ)
    binding = FormalToolBinding.discover('z3')
    assert binding.solver_path == selected and binding.suite == suite
    with binding.environment() as environment:
        actual = subprocess.check_output(('sby',), env=environment, text=True).strip()
        assert actual == 'selected'
        temporary = Path(environment['PATH'].split(os.pathsep)[0]).parent
        assert temporary.is_dir()
    assert not temporary.exists()
    assert environment_before == dict(os.environ)
    assert {p: p.read_bytes() for p in before} == before


def test_inherited_smtbmc_override_cannot_change_bound_tool(wrapped_tools, monkeypatch):
    monkeypatch.setenv('SMTBMC', 'echo wrong-tool')
    binding = FormalToolBinding.discover('z3')
    with binding.environment() as environment:
        assert subprocess.check_output(('sby',), env=environment, text=True).strip() == 'selected'


def test_frozen_binding_rejects_changed_solver_before_launch(wrapped_tools):
    _, selected = wrapped_tools
    binding = FormalToolBinding.discover('z3')
    identity = binding.identity
    _script(selected, 'echo replaced')
    with pytest.raises(FormalError, match='changed after discovery'):
        with binding.environment():
            pytest.fail('changed tool launched')
    with pytest.raises(FormalError):
        _ = binding.identity
    assert FormalToolBinding.discover('z3').identity != identity


def test_binding_identity_excludes_checkout_and_temporary_paths(wrapped_tools, tmp_path, monkeypatch):
    import shutil

    suite, selected = wrapped_tools
    first = FormalToolBinding.discover('z3').identity
    relocated = tmp_path / 'relocated'
    shutil.copytree(suite, relocated / 'suite')
    shutil.copytree(selected.parent, relocated / 'selected')
    monkeypatch.setenv('PATH', os.pathsep.join((str(relocated / 'selected'), str(relocated / 'suite/bin'), '/usr/bin', '/bin')))
    second = FormalToolBinding.discover('z3')
    assert second.identity == first
    with second.environment() as environment:
        assert subprocess.check_output(('sby',), env=environment, text=True).strip() == 'selected'


def test_same_version_binary_replacement_invalidates_selection_and_bundle_keys(wrapped_tools, monkeypatch):
    _, selected = wrapped_tools
    monkeypatch.setattr('zlang.formal_exploration.tool_versions', lambda *_: (('z3', 'same version'),))
    candidate = SimpleNamespace(implementation_identity='candidate', semantic_identity='root')
    config = FormalExplorationConfig()
    def selection_key(selected_config):
        return proof_cache_key(candidate, config=selected_config, property_identity='property',
                               artifact_hash='artifact', harness_hash='harness', assumptions_identity='none')
    first_selection = selection_key(config)
    job = SimpleNamespace(artifact_hash='artifact', backend='direct_systemverilog',
                          binding_identity='binding', route='route', selected_ir_identity='ir',
                          kind='safety', to_data=lambda: {'kind': 'safety'})
    bundle = SimpleNamespace(manifest=SimpleNamespace(bundle_identity='bundle', computed_identity='bundle'))
    def bundle_key():
        return verification_result_cache_key(bundle, job, config=VerificationRunConfig(),
                                             tool_versions=(('z3', 'same version'),))
    first_bundle = bundle_key()
    _script(selected, 'echo different-binary')
    with pytest.raises(FormalError, match='changed after discovery'):
        selection_key(config)
    assert selection_key(FormalExplorationConfig()) != first_selection
    assert bundle_key() != first_bundle


@pytest.mark.parametrize('mutation', ('mixed', 'mixed_yosys', 'incomplete'))
def test_unbindable_oss_launcher_fails_closed(wrapped_tools, tmp_path, monkeypatch, mutation):
    suite, selected = wrapped_tools
    if mutation in ('mixed', 'mixed_yosys'):
        other = tmp_path / 'other/bin'
        name = 'sby' if mutation == 'mixed' else 'yosys'
        _script(other / name, (suite / 'bin' / name).read_text().split('\n', 1)[1])
        monkeypatch.setenv('PATH', os.pathsep.join((str(other), str(selected.parent), str(suite / 'bin'), '/usr/bin', '/bin')))
    else:
        (suite / 'libexec/yosys-smtbmc').unlink()
    with pytest.raises(FormalError, match='mixed or incomplete'):
        FormalToolBinding.discover('z3')
