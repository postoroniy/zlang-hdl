"""Clock/reset-domain ownership for formal goals and backend bindings."""

from __future__ import annotations

from dataclasses import replace

from zlang.formal_domain import (
    POWER_UP_FORMAL_DOMAIN_REASON,
    formal_domain_applicability_reason,
)
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.formal_models import (
    CoverProperty,
    FormalDesign,
    FormalError,
    FormalProperty,
)
from zlang.ir.module import Module


def _formal_domain_support_reason(
    domain: ClockDomain,
    domains: tuple[ClockDomain, ...],
) -> str | None:
    """Return the bounded executable-domain exclusion, if any.

    Existing per-goal multi-domain synchronous planning remains valid.  The
    async extension is deliberately one-domain: an unrelated async domain must
    not poison a synchronous goal, but a goal sampled by that async domain is
    not executable until cross-domain reset epochs have a frozen model.
    """

    return formal_domain_applicability_reason(
        domain,
        domain_count=len(domains),
    )


def _formal_item_domain(item: FormalProperty | CoverProperty, module: Module):
    """Resolve one goal to exactly one semantic clock/reset domain.

    Formal applicability belongs to the sampled goal, not to every physical
    domain declared by the surrounding module.  In particular, an unrelated
    asynchronous domain must not disable a goal sampled by a legacy domain.
    """

    domains = module.clock_domains
    if (
        not domains
        and module.clock is not None
        and module.reset is not None
    ):
        # Retain compatibility with hand-constructed pre-domain typed modules.
        # The historical clock/reset pair denotes exactly the legacy contract.
        domains = (ClockDomain(module.clock, module.reset),)
    matches = tuple(
        domain for domain in domains
        if domain.clock == item.clock
        and (
            item.reset_condition is None
            or domain.reset == item.reset_condition
        )
    )
    if len(matches) == 1:
        return matches[0], None
    reset = (
        "<unspecified>"
        if item.reset_condition is None else item.reset_condition
    )
    if not matches:
        return None, (
            f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
            "does not resolve to a module clock/reset domain"
        )
    return None, (
        f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
        "resolves ambiguously to multiple module domains"
    )


def mark_formal_domain_applicability(
    design: FormalDesign,
    module: Module,
) -> FormalDesign:
    """Mark exact safety verification clock/reset support independently for every source goal.

    The checker supports every validated single-domain edge/polarity/assertion
    contract carried by :class:`ClockDomain`, including the frozen two-cycle
    synchronized release.  Power-up initialization remains deliberately
    outside executable formal semantics.
    """

    domains = tuple(module.clock_domains)
    if (
        not domains
        and module.clock is not None
        and module.reset is not None
    ):
        domains = (ClockDomain(module.clock, module.reset),)
    domain_blocked: set[str] = set()

    def mark(item: FormalProperty | CoverProperty):
        if item.non_executable_reason is not None:
            return item
        domain, reason = _formal_item_domain(item, module)
        if reason is None and domain is not None:
            reason = _formal_domain_support_reason(domain, domains)
        if reason is None:
            return item
        domain_blocked.add(item.id)
        return replace(item, non_executable_reason=reason)

    properties = tuple(mark(item) for item in design.properties)
    covers = tuple(mark(item) for item in design.covers)
    original_executable = tuple(
        item for item in (*design.properties, *design.covers)
        if item.non_executable_reason is None
    )
    all_domain_blocked = bool(original_executable) and all(
        item.id in domain_blocked for item in original_executable
    )
    top_reason = design.non_executable_reason
    if all_domain_blocked:
        reasons = {
            item.non_executable_reason
            for item in (*properties, *covers)
            if item.id in domain_blocked
        }
        top_reason = (
            POWER_UP_FORMAL_DOMAIN_REASON
            if reasons == {POWER_UP_FORMAL_DOMAIN_REASON}
            else "all formal goals have unsupported or unresolved clock/reset domains"
        )
    return replace(
        design,
        properties=properties,
        covers=covers,
        non_executable_reason=top_reason,
    )


def _clock_domain_from_physical(item: object) -> ClockDomain:
    """Restore and validate one v10 physical-domain manifest record."""

    try:
        return ClockDomain(
            str(getattr(item, "clock")),
            str(getattr(item, "reset")),
            ClockEdge(str(getattr(item, "clock_edge"))),
            ResetMode(str(getattr(item, "reset_mode"))),
            ResetPolarity(str(getattr(item, "reset_polarity"))),
            PowerUpPolicy(str(getattr(item, "power_up"))),
            source_origin=getattr(item, "source_origin", None),
            reset_release_mode=ResetReleaseMode(
                str(getattr(item, "reset_release_mode"))
            ),
            reset_release_cycles=getattr(item, "reset_release_cycles"),
        )
    except (TypeError, ValueError) as error:
        raise FormalError(
            f"backend artifact publishes an invalid physical reset contract: {error}"
        ) from error


def _formal_item_domain_from_design(
    item: FormalProperty | CoverProperty,
    design: FormalDesign,
) -> tuple[ClockDomain | None, str | None]:
    domains = design.clock_domains
    if not domains:
        # Compatibility with hand-constructed FormalDesign values predating
        # exact domain retention.  Their historical meaning is the legacy
        # contract named directly by the property.
        reset = item.reset_condition or "reset"
        return ClockDomain(item.clock, reset), None
    matches = tuple(
        domain for domain in domains
        if domain.clock == item.clock
        and (
            item.reset_condition is None
            or domain.reset == item.reset_condition
        )
    )
    reset = item.reset_condition or "<unspecified>"
    if len(matches) == 1:
        reason = _formal_domain_support_reason(matches[0], domains)
        return (None, reason) if reason is not None else (matches[0], None)
    if not matches:
        return None, (
            f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
            "does not resolve to its retained source domain"
        )
    return None, (
        f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
        "resolves ambiguously to retained source domains"
    )


def _physical_domain_for_formal_item(
    item: FormalProperty | CoverProperty,
    physical_domains: tuple[object, ...],
    source_domain: ClockDomain,
) -> tuple[object | None, str | None]:
    """Resolve one source goal against version-10 physical-domain records.

    Empty physical-domain metadata is the intentionally retained legacy
    manifest spelling and therefore does not by itself make a goal
    unavailable.  When records are present they contain every physical domain,
    so an exact match is mandatory.
    """

    if not physical_domains:
        if source_domain.is_legacy_default:
            return None, None
        return None, (
            f"formal goal '{item.id}' requires an exact non-default physical "
            "domain manifest"
        )
    matches = tuple(
        domain for domain in physical_domains
        if item.clock in {
            getattr(domain, "clock", None),
            getattr(domain, "rtl_clock_path", None),
        }
        and (
            item.reset_condition is None
            or item.reset_condition in {
                getattr(domain, "reset", None),
                getattr(domain, "rtl_reset_path", None),
            }
        )
    )
    reset = (
        "<unspecified>"
        if item.reset_condition is None else item.reset_condition
    )
    if not matches:
        return None, (
            f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
            "does not resolve to a published physical domain"
        )
    if len(matches) != 1:
        return None, (
            f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
            "resolves ambiguously to published physical domains"
        )
    physical_domain = _clock_domain_from_physical(matches[0])
    source_contract = replace(source_domain, source_origin=None)
    physical_contract = replace(physical_domain, source_origin=None)
    if physical_contract != source_contract:
        return matches[0], (
            f"formal goal '{item.id}' source and backend physical reset "
            "contracts do not match"
        )
    if physical_domain.power_up is not PowerUpPolicy.UNSPECIFIED:
        return matches[0], POWER_UP_FORMAL_DOMAIN_REASON
    return matches[0], None


def _binding_role(item: object) -> str:
    role = getattr(item, "role", "")
    return str(getattr(role, "value", role))


def _public_domain_bindings(
    item: FormalProperty | CoverProperty,
    public: tuple[object, ...],
) -> tuple[object | None, object | None, str | None]:
    clocks = tuple(
        binding for binding in public
        if _binding_role(binding) == "clock"
        and getattr(binding, "physical_available", False)
        and bool(getattr(binding, "rtl_path", None))
        and item.clock in {
            getattr(binding, "clock_domain", None),
            getattr(binding, "rtl_path", None),
            getattr(binding, "semantic_signal_id", None),
        }
        and (
            item.reset_condition is None
            or getattr(binding, "reset_domain", None) == item.reset_condition
        )
    )
    reset = (
        "<unspecified>"
        if item.reset_condition is None else item.reset_condition
    )
    if len(clocks) != 1:
        qualifier = "no" if not clocks else "multiple"
        return None, None, (
            f"backend artifact has {qualifier} physical clock binding for "
            f"goal domain '{item.clock}/{reset}'"
        )
    expected_reset = (
        item.reset_condition
        if item.reset_condition is not None
        else getattr(clocks[0], "reset_domain", None)
    )
    if expected_reset is None:
        return clocks[0], None, None
    resets = tuple(
        binding for binding in public
        if _binding_role(binding) == "reset"
        and getattr(binding, "physical_available", False)
        and bool(getattr(binding, "rtl_path", None))
        and getattr(binding, "clock_domain", None)
        == getattr(clocks[0], "clock_domain", None)
        and expected_reset in {
            getattr(binding, "reset_domain", None),
            getattr(binding, "rtl_path", None),
            getattr(binding, "semantic_signal_id", None),
        }
    )
    if len(resets) != 1:
        qualifier = "no" if not resets else "multiple"
        return None, None, (
            f"backend artifact has {qualifier} physical reset binding for "
            f"goal domain '{item.clock}/{expected_reset}'"
        )
    return clocks[0], resets[0], None
