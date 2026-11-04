# SPDX-License-Identifier: Apache-2.0
"""Real executed safety evidence is invalidated by changed loaded-bundle bytes."""
import pytest
from tests.semantic.test_verification_replay_input_integrity import _bundle
from zlang.ir.formal import ProofMode
from zlang.verification_bundle import (
    VerificationBundleError, VerificationRunConfig, run_verification_bundle,
)


@pytest.mark.parametrize("maps", (False, True))
def test_real_proof_cache_rechecks_loaded_harness_before_reuse(tmp_path, monkeypatch, maps):
    harness = b"""module count_within_formal(input clk, input [7:0] a);
wire [7:0] y=a^8'haa;
always @(posedge clk) assert((y^8'haa)==a);
endmodule
"""
    loaded = _bundle(tmp_path / "bundle", maps, harness=harness)
    config = VerificationRunConfig(mode=ProofMode.PROVE, depth=8, timeout_seconds=30)
    cache = tmp_path / "cache"
    cold = run_verification_bundle(loaded, config=config, cache_directory=cache)
    assert cold.results[0].status == "proven", cold.results[0].reason

    def unexpected_execution(*_args, **_kwargs):
        pytest.fail("unchanged warm replay must use its exact freshly executed proof")

    monkeypatch.setattr("zlang.verification_bundle_execution.run_verilog_formal", unexpected_execution)
    warm = run_verification_bundle(loaded, config=config, cache_directory=cache)
    assert warm.results[0].status == "proven"
    path = loaded.directory / "harness/count_within.sv"
    before = path.read_bytes()
    assert b"assert" in before
    path.write_bytes(before.replace(b"assert", b"assume"))
    with pytest.raises(VerificationBundleError, match="hash|manifest"):
        run_verification_bundle(loaded, config=config, cache_directory=cache)
