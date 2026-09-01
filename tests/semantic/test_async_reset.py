from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from zlang.ast.nodes import ClockPhysicalDecl, ResetPhysicalDecl
from zlang.ir.cdc import (
    ClockEdge,
    ClockDomain,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.opt import CanonicalizationError, canonical_ir_identity, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


CONCISE = """
module AsyncCounter {
  clock clk
  async reset arst_n @clk { polarity active_low }
  in x : u8
  out y : u8
  reg q : u8 = 0
  q <- x
  y = q
}
"""


def test_concise_async_reset_has_exact_typed_and_canonical_contract() -> None:
    module = analyze(parse(CONCISE))
    domain = module.clock_domains[0]

    assert domain.reset_mode is ResetMode.ASYNCHRONOUS
    assert domain.reset_polarity is ResetPolarity.ACTIVE_LOW
    assert domain.reset_release_mode is ResetReleaseMode.SYNCHRONIZED
    assert domain.reset_release_cycles == 2
    assert not domain.is_legacy_default
    assert restore(lower(module)) == module
    assert restore(lower(module)).clock_domains[0].source_origin == domain.source_origin


def test_falling_edge_is_retained_with_synchronized_async_release() -> None:
    module = analyze(parse(CONCISE.replace("clock clk", "clock clk { edge falling }")))

    assert module.clock_domains[0].edge is ClockEdge.FALLING
    assert module.clock_domains[0].reset_release_cycles == 2


def test_named_interface_inherits_the_exact_release_contract() -> None:
    source = """
interface AsyncIfc {
  clock clk
  async reset arst_n @clk { polarity active_low }
  in x : u8
  out y : u8
}
module Async : AsyncIfc { y = x }
"""
    module = analyze(parse(source))

    assert module.module_signature is not None
    assert module.module_signature.clock_domains == module.clock_domains
    assert module.clock_domains[0].reset_release_mode is ResetReleaseMode.SYNCHRONIZED


def test_release_contract_participates_in_canonical_identity() -> None:
    synchronized = analyze(parse(CONCISE))
    raw = analyze(parse(CONCISE.replace(
        "async reset arst_n @clk { polarity active_low }",
        "reset arst_n @clk { mode asynchronous polarity active_low "
        "power_up unspecified }",
    )))

    assert synchronized.clock_domains[0].reset_mode == raw.clock_domains[0].reset_mode
    assert synchronized.clock_domains[0].reset_release_mode != raw.clock_domains[0].reset_release_mode
    assert canonical_ir_identity(lower(synchronized)) != canonical_ir_identity(lower(raw))


@pytest.mark.parametrize(
    ("mode", "release", "cycles", "message"),
    (
        (
            ResetMode.SYNCHRONOUS,
            ResetReleaseMode.SYNCHRONIZED,
            2,
            "requires asynchronous assertion",
        ),
        (
            ResetMode.ASYNCHRONOUS,
            ResetReleaseMode.SYNCHRONIZED,
            1,
            "exactly two cycles",
        ),
        (
            ResetMode.ASYNCHRONOUS,
            ResetReleaseMode.NATIVE,
            2,
            "requires zero release cycles",
        ),
    ),
)
def test_malformed_typed_release_contract_is_rejected(
    mode: ResetMode,
    release: ResetReleaseMode,
    cycles: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ClockDomain(
            "clk",
            "rst",
            reset_mode=mode,
            reset_release_mode=release,
            reset_release_cycles=cycles,
        )


@pytest.mark.parametrize(
    ("release", "cycles", "message"),
    (
        ("eventually", 2, "release mode is not recognized"),
        (ResetReleaseMode.SYNCHRONIZED, True, "cycles must be an integer"),
    ),
)
def test_malformed_release_metadata_types_are_rejected(
    release: object,
    cycles: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ClockDomain(
            "clk",
            "rst",
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_release_mode=release,  # type: ignore[arg-type]
            reset_release_cycles=cycles,  # type: ignore[arg-type]
        )


def test_malformed_canonical_release_contract_is_rejected_on_restore() -> None:
    canonical = lower(analyze(parse(CONCISE)))
    domain = copy.deepcopy(canonical.clock_domains[0])
    object.__setattr__(domain, "reset_release_cycles", 1)
    object.__setattr__(canonical, "clock_domains", (domain,))

    with pytest.raises(
        CanonicalizationError,
        match="canonical physical clock/reset contract.*exactly two cycles",
    ):
        restore(canonical)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("edge", "both", "clock edge is not recognized"),
        ("reset_mode", "sometimes", "reset mode is not recognized"),
        ("reset_polarity", "either", "reset polarity is not recognized"),
        ("power_up", "maybe", "power-up policy is not recognized"),
    ),
)
def test_malformed_canonical_physical_domain_enums_are_rejected_on_restore(
    field: str,
    value: object,
    message: str,
) -> None:
    canonical = lower(analyze(parse(CONCISE)))
    domain = copy.deepcopy(canonical.clock_domains[0])
    object.__setattr__(domain, field, value)
    object.__setattr__(canonical, "clock_domains", (domain,))

    with pytest.raises(
        CanonicalizationError,
        match=f"canonical physical clock/reset contract.*{message}",
    ):
        restore(canonical)


def test_malformed_canonical_child_reset_contract_is_rejected_on_restore() -> None:
    source = """
module Child {
  clock clk
  async reset arst @clk
  out y : u8
  reg q : u8 = 0
  q <- q
  y = q
}
module Top {
  clock clk
  async reset arst @clk
  out y : u8
  child : Child
  y = child.y
}
"""
    canonical = lower(analyze(parse(source)))
    child = canonical.children[0]
    bad_domain = replace(
        child.clock_domains[0], reset_polarity=ResetPolarity.ACTIVE_LOW
    )
    malformed = replace(
        canonical,
        children=(replace(child, clock_domains=(bad_domain,)),),
    )

    with pytest.raises(
        CanonicalizationError,
        match="child 'child' physical clock/reset contract",
    ):
        restore(malformed)


def test_malformed_canonical_named_interface_reset_contract_is_rejected() -> None:
    source = """
interface SafeIfc {
  clock clk
  async reset arst @clk
  in x : u8
  out y : u8
}
module Safe : SafeIfc { y = x }
"""
    canonical = lower(analyze(parse(source)))
    malformed = replace(
        canonical,
        clock_domains=(replace(
            canonical.clock_domains[0],
            reset_polarity=ResetPolarity.ACTIVE_LOW,
        ),),
    )

    with pytest.raises(
        CanonicalizationError,
        match="module signature physical clock/reset contract disagrees",
    ):
        restore(malformed)


def test_programmatic_invalid_source_contract_becomes_semantic_diagnostic() -> None:
    syntax = parse("module Bad { clock clk reset rst }")
    bad_reset = ResetPhysicalDecl(
        "rst",
        None,
        "synchronous",
        "active_high",
        "unspecified",
        release_mode="synchronized",
        release_cycles=2,
    )
    syntax = replace(
        syntax,
        clock_physical=(ClockPhysicalDecl("clk"),),
        reset_physical=(bad_reset,),
        ordered_items=(
            ("clock", "clk", ClockPhysicalDecl("clk")),
            ("reset", "rst", None, bad_reset),
        ),
    )

    with pytest.raises(SemanticError, match="invalid reset contract"):
        analyze(syntax)


@pytest.mark.parametrize(
    ("child_clock", "child_reset"),
    (
        ("clock clk { edge falling }", "async reset arst @clk"),
        ("clock clk", "async reset arst @clk { polarity active_low }"),
        (
            "clock clk",
            "reset arst @clk { mode asynchronous polarity active_high "
            "power_up unspecified }",
        ),
    ),
)
def test_parent_and_sequential_child_require_the_exact_physical_contract(
    child_clock: str,
    child_reset: str,
) -> None:
    source = """
module Child {
  $CHILD_CLOCK
  $CHILD_RESET
  out y : u8
  reg q : u8 = 0
  q <- q
  y = q
}

module Top {
  clock clk
  async reset arst @clk
  out y : u8
  child : Child
  y = child.y
}
""".replace("$CHILD_CLOCK", child_clock).replace("$CHILD_RESET", child_reset)

    with pytest.raises(SemanticError, match="physical clock/reset contract"):
        analyze(parse(source))


def test_one_domain_cannot_declare_both_sync_and_async_resets() -> None:
    source = "module Bad { clock clk reset rst @clk async reset arst @clk }"
    with pytest.raises(SemanticError, match="requires exactly one reset"):
        analyze(parse(source))


def test_multi_domain_asynchronous_reset_fails_closed() -> None:
    source = """
module BadCdcReset {
  clock source
  async reset source_reset @source
  clock destination
  reset destination_reset @destination
}
"""

    with pytest.raises(
        SemanticError, match="multi-domain asynchronous reset is not supported"
    ):
        analyze(parse(source))


def test_unused_named_interface_cannot_hide_multidomain_async_reset() -> None:
    source = """
interface BadResetIfc {
  clock source
  async reset source_reset @source
  clock destination
  reset destination_reset @destination
}

module Unrelated { in x : u8 out y : u8 y = x }
"""

    with pytest.raises(
        SemanticError, match="multi-domain asynchronous reset is not supported"
    ):
        analyze(parse(source))
