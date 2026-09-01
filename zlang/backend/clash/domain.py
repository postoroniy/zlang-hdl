"""Physical clock/reset rendering shared by Clash emission plans."""

from __future__ import annotations

from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.module import Module


class ClashDomainError(ValueError):
    """Raised when a typed physical domain has no bounded Clash lowering."""


def module_domain(module: Module) -> ClockDomain:
    """Return the one physical domain supported by ordinary Clash emitters."""

    if len(module.clock_domains) != 1:
        raise ClashDomainError(
            f"module '{module.name}' requires exactly one physical clock/reset "
            "domain for Clash emission"
        )
    domain = module.clock_domains[0]
    if domain.power_up is PowerUpPolicy.RESET:
        raise ClashDomainError(
            "Clash does not yet publish power_up reset contracts"
        )
    return domain


def domain_declaration(module: Module, name: str) -> str:
    """Render one ``createDomain`` declaration from typed metadata.

    Keep the legacy/default text exact because a large compatibility surface
    deliberately snapshots generated Clash.  Non-default contracts spell out
    every physical field instead of relying on ``vSystem`` defaults.
    """

    domain = module_domain(module)
    if domain.is_legacy_default:
        return (
            f'createDomain vSystem{{vName="{name}", '
            "vResetKind=Synchronous}"
        )
    edge = "Rising" if domain.edge is ClockEdge.RISING else "Falling"
    reset_kind = (
        "Synchronous"
        if domain.reset_mode is ResetMode.SYNCHRONOUS
        else "Asynchronous"
    )
    polarity = (
        "ActiveHigh"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else "ActiveLow"
    )
    return (
        f'createDomain vSystem{{vName="{name}", vActiveEdge={edge}, '
        f"vResetKind={reset_kind}, vResetPolarity={polarity}}}"
    )


def top_reset_expression(module: Module) -> str:
    """Return the reset passed into the root circuit.

    The top wrapper owns the only release synchronizer.  Hidden child
    functions inherit that conditioned ``Reset`` and therefore cannot create
    sibling-local release epochs.
    """

    domain = module_domain(module)
    if domain.reset_release_mode is ResetReleaseMode.SYNCHRONIZED:
        return f"(resetSynchronizer {domain.clock} {domain.reset})"
    return domain.reset


__all__ = [
    "ClashDomainError",
    "domain_declaration",
    "module_domain",
    "top_reset_expression",
]
