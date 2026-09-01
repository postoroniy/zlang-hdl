"""Exact physical reset-domain rendering shared by formal products."""

from __future__ import annotations

import pytest

from zlang.formal_domain import (
    FormalDomainRenderingError,
    formal_domain_applicability_reason,
    render_formal_domain,
)
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
    clock_domain_data,
    clock_domain_from_data,
)


def test_legacy_domain_preserves_existing_formal_spellings() -> None:
    used = {"clock", "reset", "dut"}
    rendered = render_formal_domain(
        ClockDomain("clk", "rst"),
        clock_name="clock",
        reset_name="reset",
        used_names=used,
    )

    assert rendered.active_edge == "posedge"
    assert rendered.sample_event == "posedge clock"
    assert rendered.asynchronous_assertion_event is None
    assert rendered.history_event == "posedge clock"
    assert rendered.external_reset_asserted == "reset"
    assert rendered.external_reset_deasserted == "!reset"
    assert rendered.reset_active == "reset"
    assert rendered.initial_assumption == "initial assume(reset);"
    assert rendered.support_lines == ()
    assert rendered.release_tracker_name is None
    assert rendered.reset_active_name is None
    assert used == {"clock", "reset", "dut"}


@pytest.mark.parametrize(
    ("clock", "reset", "message"),
    (
        ("", "rst", "clock must be a non-empty string"),
        ("clk", "", "reset must be a non-empty string"),
        ("same", "same", "must be distinct signals"),
    ),
)
def test_clock_domain_rejects_unserializable_or_aliased_signal_names(
    clock: str,
    reset: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ClockDomain(clock, reset)


def test_exact_clock_domain_codec_round_trips_its_constructor_invariants() -> None:
    domain = ClockDomain(
        "clk",
        "arst_n",
        edge=ClockEdge.FALLING,
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_polarity=ResetPolarity.ACTIVE_LOW,
        reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
        reset_release_cycles=2,
    )
    assert clock_domain_from_data(clock_domain_data(domain)) == domain


def test_falling_edge_synchronous_active_low_is_normalized_without_async_event() -> None:
    rendered = render_formal_domain(
        ClockDomain(
            "clk",
            "rst_n",
            edge=ClockEdge.FALLING,
            reset_polarity=ResetPolarity.ACTIVE_LOW,
        ),
        clock_name="physical_clk",
        reset_name="physical_rst_n",
        used_names=set(),
    )

    assert rendered.sample_event == "negedge physical_clk"
    assert rendered.history_event == rendered.sample_event
    assert rendered.asynchronous_assertion_event is None
    assert rendered.external_reset_asserted == "!physical_rst_n"
    assert rendered.external_reset_deasserted == "physical_rst_n"
    assert rendered.reset_active == "zlang_formal_reset_active"
    assert rendered.initial_assumption == "initial assume(!physical_rst_n);"
    assert rendered.support_lines == (
        "wire zlang_formal_reset_active;",
        "assign zlang_formal_reset_active = !physical_rst_n;",
    )


@pytest.mark.parametrize(
    ("polarity", "reset_name", "asserted", "deasserted", "reset_event"),
    (
        (
            ResetPolarity.ACTIVE_HIGH,
            "arst",
            "arst",
            "!arst",
            "posedge arst",
        ),
        (
            ResetPolarity.ACTIVE_LOW,
            "arst_n",
            "!arst_n",
            "arst_n",
            "negedge arst_n",
        ),
    ),
)
def test_native_async_domain_adds_only_raw_assertion_to_history_event(
    polarity: ResetPolarity,
    reset_name: str,
    asserted: str,
    deasserted: str,
    reset_event: str,
) -> None:
    rendered = render_formal_domain(
        ClockDomain(
            "clk",
            "rst",
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_polarity=polarity,
        ),
        clock_name="clk_i",
        reset_name=reset_name,
        used_names=set(),
    )

    assert rendered.sample_event == "posedge clk_i"
    assert rendered.asynchronous_assertion_event == reset_event
    assert rendered.history_event == f"posedge clk_i or {reset_event}"
    assert rendered.external_reset_asserted == asserted
    assert rendered.external_reset_deasserted == deasserted
    if polarity is ResetPolarity.ACTIVE_HIGH:
        assert rendered.reset_active == asserted
        assert rendered.support_lines == ()
    else:
        assert rendered.reset_active == "zlang_formal_reset_active"
        assert rendered.support_lines == (
            "wire zlang_formal_reset_active;",
            "assign zlang_formal_reset_active = !arst_n;",
        )


@pytest.mark.parametrize(
    (
        "edge",
        "polarity",
        "sample_event",
        "reset_event",
        "asserted",
    ),
    (
        (
            ClockEdge.RISING,
            ResetPolarity.ACTIVE_HIGH,
            "posedge clk",
            "posedge arst",
            "arst",
        ),
        (
            ClockEdge.FALLING,
            ResetPolarity.ACTIVE_LOW,
            "negedge clk",
            "negedge arst_n",
            "!arst_n",
        ),
    ),
)
def test_synchronized_release_renders_normalized_two_stage_checker_tracker(
    edge: ClockEdge,
    polarity: ResetPolarity,
    sample_event: str,
    reset_event: str,
    asserted: str,
) -> None:
    reset_name = "arst" if polarity is ResetPolarity.ACTIVE_HIGH else "arst_n"
    used = {"clk", reset_name}
    rendered = render_formal_domain(
        ClockDomain(
            "clk",
            "rst",
            edge=edge,
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_polarity=polarity,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        ),
        clock_name="clk",
        reset_name=reset_name,
        used_names=used,
    )

    tracker = "zlang_formal_reset_release"
    effective = "zlang_formal_reset_active"
    assert rendered.sample_event == sample_event
    assert rendered.asynchronous_assertion_event == reset_event
    assert rendered.history_event == f"{sample_event} or {reset_event}"
    assert rendered.reset_active == effective
    assert rendered.release_tracker_name == tracker
    assert rendered.reset_active_name == effective
    assert rendered.support_lines == (
        f'(* ASYNC_REG = "TRUE" *) reg [1:0] {tracker};',
        f"wire {effective};",
        f"initial {tracker} = 2'b11;",
        f"always @({sample_event} or {reset_event}) begin",
        f"  if ({asserted}) {tracker} <= 2'b11;",
        f"  else {tracker} <= {{{tracker}[0], 1'b0}};",
        "end",
        f"assign {effective} = {asserted} || {tracker}[1];",
    )
    assert used == {"clk", reset_name, tracker, effective}


def test_private_tracker_names_are_collision_safe_and_deterministic() -> None:
    domain = ClockDomain(
        "clk",
        "rst",
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
        reset_release_cycles=2,
    )
    occupied = {
        "zlang_formal_reset_release",
        "zlang_formal_reset_active",
    }

    first = render_formal_domain(
        domain,
        clock_name="clk",
        reset_name="arst",
        used_names=set(occupied),
    )
    second = render_formal_domain(
        domain,
        clock_name="clk",
        reset_name="arst",
        used_names=set(occupied),
    )

    assert first.release_tracker_name == second.release_tracker_name
    assert first.reset_active_name == second.reset_active_name
    assert first.support_lines == second.support_lines
    assert first.release_tracker_name is not None
    assert first.release_tracker_name.startswith(
        "zlang_formal_reset_release__"
    )
    assert first.reset_active_name is not None
    assert first.reset_active_name.startswith("zlang_formal_reset_active__")


def test_power_up_reset_and_invalid_physical_tokens_fail_closed() -> None:
    with pytest.raises(FormalDomainRenderingError, match="power_up reset"):
        render_formal_domain(
            ClockDomain("clk", "rst", power_up=PowerUpPolicy.RESET),
            clock_name="clk",
            reset_name="rst",
            used_names=set(),
        )

    domain = ClockDomain("clk", "rst")
    with pytest.raises(FormalDomainRenderingError, match="non-empty token"):
        render_formal_domain(
            domain,
            clock_name="",
            reset_name="rst",
            used_names=set(),
        )
    with pytest.raises(FormalDomainRenderingError, match="must be distinct"):
        render_formal_domain(
            domain,
            clock_name="same",
            reset_name="same",
            used_names=set(),
        )
    with pytest.raises(FormalDomainRenderingError, match="used-name set"):
        render_formal_domain(
            domain,
            clock_name="clk",
            reset_name="rst",
            used_names={""},
        )


def test_async_applicability_is_single_domain_without_poisoning_sync_domains() -> None:
    synchronous = ClockDomain("a", "ra")
    asynchronous = ClockDomain(
        "b", "rb", reset_mode=ResetMode.ASYNCHRONOUS
    )

    assert formal_domain_applicability_reason(
        synchronous, domain_count=2
    ) is None
    assert "multi-domain" in (
        formal_domain_applicability_reason(
            asynchronous, domain_count=2
        )
        or ""
    )
    assert formal_domain_applicability_reason(
        asynchronous, domain_count=1
    ) is None
    assert "power-up" in (
        formal_domain_applicability_reason(
            ClockDomain("p", "rp", power_up=PowerUpPolicy.RESET),
            domain_count=1,
        )
        or ""
    )
    with pytest.raises(FormalDomainRenderingError, match="positive integer"):
        formal_domain_applicability_reason(synchronous, domain_count=0)

def test_corrupted_frozen_domain_is_revalidated_at_render_boundary() -> None:
    domain = ClockDomain(
        "clk",
        "rst",
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
        reset_release_cycles=2,
    )
    malformed = domain
    object.__setattr__(malformed, "reset_release_mode", ResetReleaseMode.NATIVE)

    with pytest.raises(
        FormalDomainRenderingError,
        match="native reset release requires zero release cycles",
    ):
        render_formal_domain(
            malformed,
            clock_name="clk",
            reset_name="rst",
            used_names=set(),
        )
