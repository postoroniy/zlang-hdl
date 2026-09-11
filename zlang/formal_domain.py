"""Shared physical clock/reset rendering for executable formal harnesses.

The production backends already consume :class:`~zlang.ir.cdc.ClockDomain` as
the authoritative physical reset contract.  Formal harnesses must do the same
instead of inferring an edge or polarity from an RTL port name.  This module is
deliberately independent from M35 and M36: it renders the small piece of
checker-local SystemVerilog which all three products need.

The synchronized-release registers emitted here are *checker state*.  They do
not condition the DUT reset a second time.  Their only purpose is to give
history/valid masks the same asynchronous-assert, two-active-edge-release epoch
as the root-owned DUT conditioner.
"""

from __future__ import annotations

from dataclasses import dataclass

from zlang.backend.identifiers import allocate_private_rtl_identifier
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)


class FormalDomainRenderingError(ValueError):
    """An exact physical domain cannot be represented by a formal checker."""


POWER_UP_FORMAL_DOMAIN_REASON = (
    "formal execution does not support a power-up reset contract"
)

MULTI_DOMAIN_ASYNC_FORMAL_REASON = (
    "formal execution does not support an asynchronous reset goal in a "
    "multi-domain module"
)


def formal_domain_applicability_reason(
    domain: ClockDomain,
    *,
    domain_count: int,
) -> str | None:
    """Return the shared bounded formal-domain exclusion, if any.

    Goal-local multi-domain synchronous verification predates async reset and
    remains valid.  Async applicability is intentionally single-domain until
    reset epochs that interact across domains have a frozen model.
    """

    if (
        not isinstance(domain_count, int)
        or isinstance(domain_count, bool)
        or domain_count < 1
    ):
        raise FormalDomainRenderingError(
            "formal domain count must be a positive integer"
        )
    try:
        domain.validate()
    except ValueError as error:
        raise FormalDomainRenderingError(str(error)) from error
    if domain.power_up is not PowerUpPolicy.UNSPECIFIED:
        return POWER_UP_FORMAL_DOMAIN_REASON
    if domain_count > 1 and domain.reset_mode is ResetMode.ASYNCHRONOUS:
        return MULTI_DOMAIN_ASYNC_FORMAL_REASON
    return None


@dataclass(frozen=True)
class FormalDomainRendering:
    """Deterministic SystemVerilog fragments for one exact physical domain.

    ``sample_event`` is always the declared active clock edge.  ``history_event``
    additionally includes the raw asynchronous assertion event when history
    must be cleared between clock edges.  Consumers should gate sampled goals
    with ``reset_active`` and use ``history_event`` only for checker history.

    ``support_lines`` is empty for native release.  For synchronized release it
    contains one collision-safe two-stage tracker and one normalized active-high
    effective-reset net.  ``used_names`` passed to :func:`render_formal_domain`
    is updated with those private declarations.
    """

    domain: ClockDomain
    clock_name: str
    reset_name: str
    active_edge: str
    sample_event: str
    asynchronous_assertion_event: str | None
    history_event: str
    external_reset_asserted: str
    external_reset_deasserted: str
    reset_active: str
    initial_assumption: str
    support_lines: tuple[str, ...] = ()
    release_tracker_name: str | None = None
    reset_active_name: str | None = None


def _nonempty_token(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FormalDomainRenderingError(f"formal {label} must be a non-empty token")
    if "\n" in value or "\r" in value:
        raise FormalDomainRenderingError(
            f"formal {label} cannot contain a newline"
        )
    return value


def _contract_identity(
    domain: ClockDomain,
    *,
    clock_name: str,
    reset_name: str,
) -> str:
    return "|".join(
        (
            "formal-reset-domain",
            domain.clock,
            domain.reset,
            clock_name,
            reset_name,
            domain.edge.value,
            domain.reset_mode.value,
            domain.reset_polarity.value,
            domain.power_up.value,
            domain.reset_release_mode.value,
            str(domain.reset_release_cycles),
        )
    )


def render_formal_domain(
    domain: ClockDomain,
    *,
    clock_name: str,
    reset_name: str,
    used_names: set[str],
) -> FormalDomainRendering:
    """Render the checker view of ``domain`` using explicit physical tokens.

    The caller owns the physical clock/reset names, normally obtained from a
    validated BackendArtifact binding.  The helper never derives them from the
    semantic names in ``domain``.  ``used_names`` is mutated only when private
    synchronized-release declarations are required.
    """

    if not isinstance(domain, ClockDomain):
        raise FormalDomainRenderingError(
            "formal reset rendering requires an exact ClockDomain"
        )
    # Revalidate restored or deliberately corrupted frozen instances.
    try:
        domain.validate()
    except ValueError as error:
        raise FormalDomainRenderingError(str(error)) from error
    if domain.power_up is not PowerUpPolicy.UNSPECIFIED:
        raise FormalDomainRenderingError(
            "formal reset rendering does not support power_up reset"
        )
    if not isinstance(used_names, set) or any(
        not isinstance(item, str) or not item for item in used_names
    ):
        raise FormalDomainRenderingError(
            "formal used-name set must contain only non-empty strings"
        )

    clock = _nonempty_token(clock_name, "clock name")
    reset = _nonempty_token(reset_name, "reset name")
    if clock == reset:
        raise FormalDomainRenderingError(
            "formal clock and reset physical tokens must be distinct"
        )

    active_edge = "posedge" if domain.edge is ClockEdge.RISING else "negedge"
    sample_event = f"{active_edge} {clock}"
    reset_assertion_edge = (
        "posedge"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else "negedge"
    )
    external_asserted = (
        reset
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else f"!{reset}"
    )
    external_deasserted = (
        f"!{reset}"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else reset
    )
    asynchronous_event = (
        f"{reset_assertion_edge} {reset}"
        if domain.reset_mode is ResetMode.ASYNCHRONOUS
        else None
    )
    history_event = (
        f"{sample_event} or {asynchronous_event}"
        if asynchronous_event is not None
        else sample_event
    )
    initial_assumption = f"initial assume({external_asserted});"

    if domain.reset_release_mode is ResetReleaseMode.NATIVE:
        if domain.reset_polarity is ResetPolarity.ACTIVE_LOW:
            identity = _contract_identity(
                domain,
                clock_name=clock,
                reset_name=reset,
            )
            effective = allocate_private_rtl_identifier(
                "zlang_formal_reset_active",
                semantic_identity=f"{identity}|active",
                used=used_names,
            )
            return FormalDomainRendering(
                domain,
                clock,
                reset,
                active_edge,
                sample_event,
                asynchronous_event,
                history_event,
                external_asserted,
                external_deasserted,
                effective,
                initial_assumption,
                (
                    f"wire {effective};",
                    f"assign {effective} = {external_asserted};",
                ),
                None,
                effective,
            )
        return FormalDomainRendering(
            domain,
            clock,
            reset,
            active_edge,
            sample_event,
            asynchronous_event,
            history_event,
            external_asserted,
            external_deasserted,
            external_asserted,
            initial_assumption,
        )

    # ClockDomain validation currently admits only asynchronous assertion and
    # exactly two release cycles here.  Keep the checks local as an executable
    # boundary too, so future ClockDomain extensions fail closed.
    if (
        domain.reset_mode is not ResetMode.ASYNCHRONOUS
        or domain.reset_release_cycles != 2
    ):
        raise FormalDomainRenderingError(
            "formal synchronized reset release requires asynchronous assertion "
            "and exactly two release cycles"
        )
    assert asynchronous_event is not None
    identity = _contract_identity(
        domain,
        clock_name=clock,
        reset_name=reset,
    )
    tracker = allocate_private_rtl_identifier(
        "zlang_formal_reset_release",
        semantic_identity=f"{identity}|tracker",
        used=used_names,
    )
    effective = allocate_private_rtl_identifier(
        "zlang_formal_reset_active",
        semantic_identity=f"{identity}|active",
        used=used_names,
    )
    support_lines = (
        f'(* ASYNC_REG = "TRUE" *) reg [1:0] {tracker};',
        f"wire {effective};",
        f"initial {tracker} = 2'b11;",
        f"always @({history_event}) begin",
        f"  if ({external_asserted}) {tracker} <= 2'b11;",
        f"  else {tracker} <= {{{tracker}[0], 1'b0}};",
        "end",
        f"assign {effective} = {external_asserted} || {tracker}[1];",
    )
    return FormalDomainRendering(
        domain,
        clock,
        reset,
        active_edge,
        sample_event,
        asynchronous_event,
        history_event,
        external_asserted,
        external_deasserted,
        effective,
        initial_assumption,
        support_lines,
        tracker,
        effective,
    )


__all__ = [
    "FormalDomainRendering",
    "FormalDomainRenderingError",
    "MULTI_DOMAIN_ASYNC_FORMAL_REASON",
    "POWER_UP_FORMAL_DOMAIN_REASON",
    "formal_domain_applicability_reason",
    "render_formal_domain",
]
