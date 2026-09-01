"""Exact physical-domain validation for retained M36 backend artifacts.

M36 replay is immutable only when its typed relation and both retained backend
artifacts agree about the sampled clock/reset contract.  This module keeps
that check shared by the prepared-leg codec and the frozen-site validator; it
does not add a new equivalence relation or infer any RTL names.
"""

from __future__ import annotations

from zlang.backend.manifest import (
    PHYSICAL_DOMAIN_MANIFEST_VERSION,
    BackendArtifact,
    PhysicalDomainManifest,
)
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
    clock_domain_contract_identity,
)
from zlang.ir.equivalence import (
    BindingSide,
    EquivalenceProperty,
    SignalRole,
)


class EquivalenceArtifactDomainError(ValueError):
    """A retained M36 artifact disagrees with its typed physical domain."""


def _manifest_domain(record: PhysicalDomainManifest) -> ClockDomain:
    try:
        record.validate()
        return ClockDomain(
            record.clock,
            record.reset,
            ClockEdge(record.clock_edge),
            ResetMode(record.reset_mode),
            ResetPolarity(record.reset_polarity),
            PowerUpPolicy(record.power_up),
            reset_release_mode=ResetReleaseMode(record.reset_release_mode),
            reset_release_cycles=record.reset_release_cycles,
        )
    except (TypeError, ValueError) as error:
        raise EquivalenceArtifactDomainError(
            f"physical-domain manifest is invalid: {error}"
        ) from error


def _binding_for_role(
    artifact: BackendArtifact,
    domain: ClockDomain,
    role: SignalRole,
    side: BindingSide,
    *,
    label: str,
):
    matches = tuple(
        binding
        for binding in artifact.bindings
        if binding.role is role
        and binding.side is side
        and binding.clock_domain == domain.clock
        and binding.reset_domain == domain.reset
    )
    if len(matches) != 1:
        qualifier = "no" if not matches else "multiple"
        raise EquivalenceArtifactDomainError(
            f"{label} artifact has {qualifier} exact {role.value} binding for "
            f"physical domain '{domain.clock}/{domain.reset}'"
        )
    binding = matches[0]
    if (
        not binding.physical_available
        or binding.width != 1
        or binding.rtl_module != artifact.module
        or not binding.rtl_path
    ):
        raise EquivalenceArtifactDomainError(
            f"{label} artifact {role.value} binding is not a validated one-bit "
            "public RTL path"
        )
    return binding


def validate_equivalence_artifact_domain(
    property_: EquivalenceProperty,
    artifact: BackendArtifact,
    *,
    side: BindingSide,
    label: str,
) -> None:
    """Validate one retained artifact against the exact M36 domain contract.

    Version-10 artifacts must publish one matching physical-domain record and
    the record's paths must resolve to the exact public clock/reset bindings.
    Historical artifacts without a v10 record retain only their original
    synchronous, rising-edge, active-high meaning.
    """

    expected = property_.clock_domain_contract
    records = tuple(artifact.physical_domains)
    if expected is None:
        if records:
            raise EquivalenceArtifactDomainError(
                f"{label} artifact publishes a physical clock/reset domain but "
                "the M36 property has no sampled domain contract"
            )
        return

    expected.validate()
    expected_identity = clock_domain_contract_identity(expected)
    if records:
        if artifact.manifest_version != PHYSICAL_DOMAIN_MANIFEST_VERSION:
            raise EquivalenceArtifactDomainError(
                f"{label} physical-domain artifact requires manifest version "
                f"{PHYSICAL_DOMAIN_MANIFEST_VERSION}"
            )
        if len(records) != 1:
            raise EquivalenceArtifactDomainError(
                f"{label} artifact must publish exactly one physical domain for "
                "the retained M36 relation"
            )
        record = records[0]
        actual = _manifest_domain(record)
        if actual != expected or record.identity != expected_identity:
            raise EquivalenceArtifactDomainError(
                f"{label} artifact physical clock/reset contract disagrees with "
                "the M36 property"
            )
        if record.rtl_module != artifact.module:
            raise EquivalenceArtifactDomainError(
                f"{label} physical domain belongs to a different RTL module"
            )
        clock = _binding_for_role(
            artifact, expected, SignalRole.CLOCK, side, label=label
        )
        reset = _binding_for_role(
            artifact, expected, SignalRole.RESET, side, label=label
        )
        if clock.rtl_path != record.rtl_clock_path:
            raise EquivalenceArtifactDomainError(
                f"{label} physical-domain clock path disagrees with its binding"
            )
        if reset.rtl_path != record.rtl_reset_path:
            raise EquivalenceArtifactDomainError(
                f"{label} physical-domain reset path disagrees with its binding"
            )
        return

    if artifact.manifest_version >= PHYSICAL_DOMAIN_MANIFEST_VERSION:
        raise EquivalenceArtifactDomainError(
            f"{label} manifest version {artifact.manifest_version} is missing its "
            "physical-domain record"
        )
    if not expected.is_legacy_default:
        raise EquivalenceArtifactDomainError(
            f"{label} legacy artifact cannot represent the required non-default "
            "physical clock/reset contract"
        )
    # A legacy artifact carries no separate contract record, but its exact
    # public bindings still have to identify the expected historical domain.
    _binding_for_role(
        artifact, expected, SignalRole.CLOCK, side, label=label
    )
    _binding_for_role(
        artifact, expected, SignalRole.RESET, side, label=label
    )


def validate_prepared_equivalence_domains(
    property_: EquivalenceProperty,
    reference_artifact: BackendArtifact | None,
    implementation_artifact: BackendArtifact | None,
) -> None:
    """Validate all retained artifacts of one prepared M36 leg."""

    if reference_artifact is not None:
        validate_equivalence_artifact_domain(
            property_,
            reference_artifact,
            side=BindingSide.REFERENCE,
            label="reference",
        )
    if implementation_artifact is not None:
        validate_equivalence_artifact_domain(
            property_,
            implementation_artifact,
            side=BindingSide.IMPLEMENTATION,
            label="implementation",
        )


__all__ = [
    "EquivalenceArtifactDomainError",
    "validate_equivalence_artifact_domain",
    "validate_prepared_equivalence_domains",
]
