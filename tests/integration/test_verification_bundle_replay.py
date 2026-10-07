from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

import pytest

from zlang.verification_bundle import (
    VerificationBundleInput,
    VerificationJob,
    VerificationRunConfig,
    publish_verification_bundle,
    run_verification_bundle,
    verification_identity_for,
)


FORMAL_TOOLS = all(
    shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _bundle(root: Path, *, broken: bool) -> None:
    implementation = (
        "module BundleDut(input wire x, output wire y); "
        f"assign y = {'~x' if broken else 'x'}; endmodule\n"
    )
    harness = """
module bundle_formal;
  (* anyconst *) reg x;
  wire y;
  BundleDut dut(.x(x), .y(y));
  always @* assert(y == x);
endmodule
"""
    property_id = "bundle.output_identity"
    hardware_identity = "hardware:" + _digest(implementation)
    payload = {
        "formal_ir_version": 1,
        "identities": {
            "source": "source:" + _digest(implementation),
            "dependency": "dependency:" + _digest("none"),
            "compiler": "compiler:" + _digest("test"),
        },
        "hardware": {
            "high_level_ir_identity": hardware_identity,
            "selected_ir_identity": hardware_identity,
        },
        "scopes": [],
        "properties": [{
            "id": property_id,
            "kind": "safety",
            "generated_from": None,
            "predicate": {"kind": "constant", "value": 1},
            "source_origin": None,
        }],
        "bindings": [],
        "vacuity_dependencies": {},
    }
    verification_identity = verification_identity_for(
        top="BundleDut",
        hardware_identity=hardware_identity,
        property_ids=(property_id,),
        payload=payload,
    )
    publish_verification_bundle(
        root,
        top="BundleDut",
        hardware_identity=hardware_identity,
        verification_identity=verification_identity,
        property_ids=(property_id,),
        verification_ir=payload,
        files=(
            VerificationBundleInput(
                "implementation/BundleDut.sv", "implementation", implementation.encode()
            ),
            VerificationBundleInput(
                "harness/output_identity.sv", "harness", harness.encode()
            ),
            VerificationBundleInput(
                "config/output_identity.json", "config", b'{"binding_schema":1}\n'
            ),
            VerificationBundleInput(
                "source-map/output_identity.json", "source_map", b'{"mappings":[]}\n'
            ),
        ),
        jobs=(VerificationJob(
            property_id,
            "safety",
            "bundle_formal",
            (
                "implementation/BundleDut.sv",
                "harness/output_identity.sv",
            ),
            ("config/output_identity.json",),
            ("source-map/output_identity.json",),
        ),),
    )


@pytest.mark.skipif(
    not FORMAL_TOOLS,
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_immutable_bundle_replays_real_safety_pass_and_mutation(tmp_path: Path) -> None:
    _bundle(tmp_path / "correct", broken=False)
    _bundle(tmp_path / "broken", broken=True)
    config = VerificationRunConfig(depth=2, timeout_seconds=30)

    correct = run_verification_bundle(tmp_path / "correct", config=config)
    broken = run_verification_bundle(tmp_path / "broken", config=config)

    assert correct.results[0].status == "bounded_pass"
    assert correct.exit_code == 0
    assert broken.results[0].status == "failed"
    assert broken.exit_code == 1
    assert broken.results[0].counterexample is not None
    assert broken.results[0].counterexample.property_id == "bundle.output_identity"
