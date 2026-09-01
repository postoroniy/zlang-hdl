"""Strict physical-domain validation for immutable prepared M36 legs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zlang.candidate_equivalence import FrozenCandidateEquivalenceSite
from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.formal_candidate import (
    PreparedCandidateEquivalence,
    prepared_candidate_equivalence_from_data,
    prepared_candidate_equivalence_to_data,
)
from zlang.formal_orchestration import FormalOrchestrationError
from zlang.ir import (
    Assignment,
    BitType,
    ComparisonWindow,
    Constant,
    EquivalenceProperty,
    EquivalenceRelation,
    Module,
    Port,
    PortDirection,
)
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.equivalence import BindingSide


CANDIDATE = "selected:physical-domain-candidate"


def _property(domain: ClockDomain) -> EquivalenceProperty:
    return EquivalenceProperty(
        "m36.physical-domain",
        EquivalenceRelation.FIXED_LATENCY_VALUE,
        "reference:physical-domain",
        CANDIDATE,
        BitType(),
        (),
        "port:result",
        "port:result",
        0,
        1,
        1,
        1,
        domain.clock,
        domain.clock,
        domain.reset,
        domain.reset,
        1,
        ComparisonWindow.reset_fill(
            1, reset_release_cycles=domain.reset_release_cycles
        ),
        candidate_class="m31",
        clock_domain_contract=domain,
    )


def _artifact(
    backend: str,
    module_name: str,
    domain: ClockDomain,
    side: BindingSide,
) -> BackendArtifact:
    result = Port(PortDirection.OUTPUT, "result", BitType())
    module = Module(
        module_name,
        (result,),
        (Assignment(result, Constant(0, BitType())),),
        clock=domain.clock,
        reset=domain.reset,
        clock_domains=(domain,),
    )
    text = (
        f"module {module_name}(input {domain.clock}, input {domain.reset}, "
        "output result); assign result = 1'b0; endmodule\n"
    )
    return publish_artifact(
        module,
        text,
        backend=backend,
        selected_ir_identity=CANDIDATE,
        side=side,
    )


def _bundle(domain: ClockDomain) -> PreparedCandidateEquivalence:
    reference = _artifact(
        "semantic_reference", "ReferenceDomain", domain, BindingSide.REFERENCE
    )
    implementation = _artifact(
        "direct_systemverilog",
        "ImplementationDomain",
        domain,
        BindingSide.IMPLEMENTATION,
    )
    property_ = _property(domain)
    return PreparedCandidateEquivalence(
        property_,
        "module m36_physical_domain; endmodule\n",
        "m36_physical_domain",
        reference.artifact_hash,
        implementation.artifact_hash,
        "d" * 64,
        property_.id,
        "e" * 64,
        "f" * 64,
        "direct_systemverilog",
        reference,
        implementation,
        (),
    )


def _artifact_payload(artifact: BackendArtifact) -> dict[str, object]:
    return {"manifest": json.loads(artifact.to_json()), "text": artifact.text}


def _replace_implementation_domain(
    data: dict[str, object], domain: ClockDomain
) -> dict[str, object]:
    artifact = _artifact(
        "direct_systemverilog",
        "ImplementationDomain",
        domain,
        BindingSide.IMPLEMENTATION,
    )
    data["implementation_artifact"] = _artifact_payload(artifact)
    data["implementation_artifact_hash"] = artifact.artifact_hash
    return data


def _synchronized_domain() -> ClockDomain:
    return ClockDomain(
        "clk",
        "rst_n",
        edge=ClockEdge.FALLING,
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_polarity=ResetPolarity.ACTIVE_LOW,
        reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
        reset_release_cycles=2,
    )


def test_prepared_m36_round_trip_retains_exact_v10_domain_and_paths() -> None:
    bundle = _bundle(_synchronized_domain())
    restored = prepared_candidate_equivalence_from_data(
        prepared_candidate_equivalence_to_data(bundle)
    )
    assert restored == bundle
    for artifact in (
        restored.reference_artifact,
        restored.implementation_artifact,
    ):
        assert artifact is not None
        assert artifact.manifest_version == 10
        record = artifact.physical_domains[0]
        bindings = {item.role.value: item for item in artifact.bindings}
        assert record.rtl_clock_path == bindings["clock"].rtl_path
        assert record.rtl_reset_path == bindings["reset"].rtl_path


@pytest.mark.parametrize(
    "actual",
    (
        ClockDomain(
            "clk", "rst_n", edge=ClockEdge.RISING,
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_polarity=ResetPolarity.ACTIVE_LOW,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        ),
        ClockDomain(
            "clk", "rst_n", edge=ClockEdge.FALLING,
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_polarity=ResetPolarity.ACTIVE_HIGH,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        ),
        ClockDomain(
            "clk", "rst_n", edge=ClockEdge.FALLING,
            reset_mode=ResetMode.SYNCHRONOUS,
            reset_polarity=ResetPolarity.ACTIVE_LOW,
        ),
        ClockDomain(
            "clk", "rst_n", edge=ClockEdge.FALLING,
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_polarity=ResetPolarity.ACTIVE_LOW,
        ),
        ClockDomain(
            "clk", "rst_n", edge=ClockEdge.FALLING,
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_polarity=ResetPolarity.ACTIVE_LOW,
            power_up=PowerUpPolicy.RESET,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        ),
    ),
    ids=("edge", "polarity", "mode", "release", "power_up"),
)
def test_prepared_m36_rejects_exact_contract_corruption(
    actual: ClockDomain,
) -> None:
    expected = _synchronized_domain()
    data = prepared_candidate_equivalence_to_data(_bundle(expected))
    _replace_implementation_domain(data, actual)
    with pytest.raises(ValueError, match="physical clock/reset contract disagrees"):
        prepared_candidate_equivalence_from_data(data)


def test_prepared_m36_rejects_physical_manifest_path_corruption() -> None:
    data = prepared_candidate_equivalence_to_data(_bundle(_synchronized_domain()))
    implementation = data["implementation_artifact"]
    assert isinstance(implementation, dict)
    manifest = implementation["manifest"]
    assert isinstance(manifest, dict)
    # Remove the derived build identity so restoration reaches the exact
    # record-to-binding locator check rather than failing on its enclosing hash.
    manifest.pop("build_identity", None)
    domains = manifest["physical_domains"]
    assert isinstance(domains, list)
    domains[0]["rtl_reset_path"] = "stale_reset_path"
    with pytest.raises(ValueError, match="physical domain reset locator"):
        prepared_candidate_equivalence_from_data(data)


def test_legacy_prepared_artifacts_are_accepted_only_for_legacy_domain() -> None:
    legacy = ClockDomain("clk", "rst")
    data = prepared_candidate_equivalence_to_data(_bundle(legacy))
    restored = prepared_candidate_equivalence_from_data(data)
    assert restored.reference_artifact is not None
    assert restored.reference_artifact.manifest_version < 10
    assert restored.reference_artifact.physical_domains == ()

    non_default = ClockDomain(
        "clk",
        "rst",
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_polarity=ResetPolarity.ACTIVE_HIGH,
    )
    property_data = data["property"]
    assert isinstance(property_data, dict)
    property_data["clock_domain_contract"] = {
        "clock": "clk",
        "reset": "rst",
        "edge": "rising",
        "reset_mode": "asynchronous",
        "reset_polarity": "active_high",
        "power_up": "unspecified",
        "reset_release_mode": "native",
        "reset_release_cycles": 0,
    }
    # The changed contract remains a valid fixed-latency relation, but cannot
    # be reconstructed from a pre-v10 backend artifact.
    assert _property(non_default).comparison_window.to_data() == property_data[
        "comparison_window"
    ]
    with pytest.raises(ValueError, match="legacy artifact cannot represent"):
        prepared_candidate_equivalence_from_data(data)


def test_legacy_binding_names_must_match_the_exact_property_domain() -> None:
    expected = ClockDomain("clk", "rst")
    data = prepared_candidate_equivalence_to_data(_bundle(expected))
    _replace_implementation_domain(data, ClockDomain("other_clk", "other_rst"))
    with pytest.raises(ValueError, match="no exact clock binding"):
        prepared_candidate_equivalence_from_data(data)


def test_in_memory_frozen_site_reuses_the_same_physical_domain_validator() -> None:
    expected = _synchronized_domain()
    actual = ClockDomain(
        "clk",
        "rst_n",
        edge=ClockEdge.RISING,
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_polarity=ResetPolarity.ACTIVE_LOW,
        reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
        reset_release_cycles=2,
    )
    valid = _bundle(expected)
    mismatched = PreparedCandidateEquivalence(
        valid.property,
        valid.source,
        valid.top,
        valid.reference_artifact_hash,
        _artifact(
            "direct_systemverilog",
            "ImplementationDomain",
            actual,
            BindingSide.IMPLEMENTATION,
        ).artifact_hash,
        valid.harness_hash,
        valid.property_identity,
        valid.assumptions_identity,
        valid.backend_identity,
        valid.backend,
        valid.reference_artifact,
        _artifact(
            "direct_systemverilog",
            "ImplementationDomain",
            actual,
            BindingSide.IMPLEMENTATION,
        ),
        valid.input_semantic_ids,
    )
    with pytest.raises(FormalOrchestrationError, match="physical domain is invalid"):
        FrozenCandidateEquivalenceSite._validate_leg(
            "direct_systemverilog",
            SimpleNamespace(route=None),
            valid.property,
            mismatched,
        )
