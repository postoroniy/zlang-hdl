# SPDX-License-Identifier: Apache-2.0
"""Cache admission validates published inputs independently of diagnostics."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.semantic.test_verification_bundle import _fixture, _inputs, _job, _digest
from zlang.ir.formal import FormalResult, FormalStatus, ProofMode
from zlang.formal_execution_tools import FormalToolBinding, binding_identity
from zlang.verification_bundle import (
    VerificationBundleError, VerificationBundleInput, VerificationRunConfig,
    load_verification_bundle, publish_verification_bundle, run_verification_bundle,
    verification_identity_for,
)


def _bundle(root, maps, *, harness=None):
    model = "implementation/companions/model.v"
    job = replace(_job(), source_files=(*_job().source_files, model),
                  source_map_files=_job().source_map_files if maps else ())
    hardware = "hardware:" + _digest("counter-hardware")
    fixture = _fixture(hardware, (job,))
    files = tuple(
        replace(item, content=harness if harness is not None else
                b"module count_within_formal; always @* begin "
                b"assume(1'b1); assert(1'b1); end endmodule\n")
        if item.kind == "harness" else item
        for item in _inputs() if maps or item.kind != "source_map"
    ) + (VerificationBundleInput(model, "companion", b"module model; endmodule\n"),)
    publish_verification_bundle(
        root, top="Counter", hardware_identity=hardware,
        verification_identity=verification_identity_for(
            top="Counter", hardware_identity=hardware,
            property_ids=(job.property_id,), payload=fixture.payload,
        ),
        property_ids=(job.property_id,), verification_ir=fixture.payload,
        files=files, jobs=fixture.jobs,
    )
    return load_verification_bundle(root)


def _solver(monkeypatch):
    versions = (("yosys", "test"), ("sby", "test"),
                ("yosys-smtbmc", "test"), ("z3", "test"))
    monkeypatch.setattr(
        "zlang.verification_bundle.FormalToolchainContext.discover",
        lambda **kw: SimpleNamespace(
            **kw, versions=versions,
            execution_identity=binding_identity(FormalToolBinding.discover(kw["solver"])),
        ),
    )
    calls = []

    def run(source, **kw):
        calls.append(source)
        return FormalResult(
            kw["property_id"],
            FormalStatus.PROVEN if kw["mode"] is ProofMode.PROVE else FormalStatus.BOUNDED_PASS,
            kw["mode"], kw["engine"], kw["solver"], kw["depth"],
            source_origin=kw["source_origin"], tool_versions=versions,
        )

    monkeypatch.setattr("zlang.verification_bundle_execution.run_verilog_formal", run)
    return calls


@pytest.mark.parametrize("maps", (False, True))
@pytest.mark.parametrize("loaded_entry", (False, True))
@pytest.mark.parametrize("warm", (False, True))
@pytest.mark.parametrize("mutation", ("assert", "assumption", "source", "config", "model"))
def test_changed_replay_inputs_reject_before_result_cache(
    tmp_path, monkeypatch, maps, loaded_entry, warm, mutation,
):
    loaded = _bundle(tmp_path / "bundle", maps)
    calls = _solver(monkeypatch)
    config = VerificationRunConfig(depth=8, timeout_seconds=30)
    entry = loaded if loaded_entry else loaded.directory
    cache = tmp_path / "cache"
    if warm:
        run_verification_bundle(entry, config=config, cache_directory=cache)
    paths = dict(assert_="harness/count_within.sv", assumption="harness/count_within.sv",
                 source="implementation/Counter.sv", config="config/count_within.json",
                 model="implementation/companions/model.v")
    path = loaded.directory / paths["assert_" if mutation == "assert" else mutation]
    before = path.read_bytes()
    after = before + b"\n// changed input\n"
    if mutation == "assert":
        after = before.replace(b"assert", b"assume")
    elif mutation == "assumption":
        after = before.replace(b"assume(1'b1)", b"assume(1'b0)")
    elif mutation == "config":
        after = before.replace(b'"binding_schema":1', b'"binding_schema":2')
    assert after != before
    path.write_bytes(after)
    with pytest.raises(VerificationBundleError, match="hash|manifest"):
        run_verification_bundle(entry, config=config, cache_directory=cache)
    assert len(calls) == int(warm)


@pytest.mark.parametrize("maps", (False, True))
@pytest.mark.parametrize("mode", (ProofMode.BMC, ProofMode.PROVE))
def test_unchanged_replay_reuses_exact_status_without_fresh_execution(tmp_path, monkeypatch, maps, mode):
    loaded = _bundle(tmp_path / "bundle", maps)
    calls = _solver(monkeypatch)
    config = VerificationRunConfig(mode=mode, depth=8, timeout_seconds=30)
    reports = [run_verification_bundle(loaded, config=config, cache_directory=tmp_path / "cache")
               for _ in range(2)]
    assert len(calls) == 1
    expected = "proven" if mode is ProofMode.PROVE else "bounded_pass"
    assert [r.results[0].status for r in reports] == [expected, expected]


def test_loaded_ir_cannot_override_validated_snapshot(tmp_path, monkeypatch):
    loaded = _bundle(tmp_path / "bundle", False)
    calls = _solver(monkeypatch)
    changed = replace(loaded, verification_ir={**loaded.verification_ir, "top": "Changed"})
    with pytest.raises(VerificationBundleError, match="changed since it was loaded"):
        run_verification_bundle(changed, cache_directory=tmp_path / "cache")
    assert not calls
