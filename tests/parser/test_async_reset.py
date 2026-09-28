from __future__ import annotations

import pytest

from zlang.parser import ParseError, parse


def test_concise_async_reset_defaults_to_active_high_synchronized_release() -> None:
    module = parse("module Async { clock clk async reset arst @clk }")

    assert module.resets == ("arst",)
    assert module.reset_domains == (("arst", "clk"),)
    reset = module.reset_physical[0]
    assert reset.mode == "asynchronous"
    assert reset.polarity == "active_high"
    assert reset.power_up == "unspecified"
    assert reset.release_mode == "synchronized"
    assert reset.release_cycles == 2


def test_concise_async_reset_accepts_only_the_polarity_override() -> None:
    module = parse(
        "module AsyncLow { "
        "clock clk async reset arst_n @clk { polarity active_low } "
        "}"
    )

    assert module.reset_physical[0].polarity == "active_low"


def test_async_reset_can_declare_externally_synchronized_release() -> None:
    module = parse(
        "module ExternalSync { clock clk async reset arst_n @clk { "
        "release externally_synchronized polarity active_low } }"
    )

    reset = module.reset_physical[0]
    assert reset.mode == "asynchronous"
    assert reset.polarity == "active_low"
    assert reset.release_mode == "externally_synchronized"
    assert reset.release_cycles == 0


def test_register_reset_value_is_optional() -> None:
    module = parse(
        "module OptionalReset { clock clk reset rst @clk "
        "reg reset_q:u8=0 reg free_q:u8 free_q <- reset_q }"
    )

    reset_q, free_q = module.registers
    assert reset_q.initial is not None
    assert free_q.initial is None


@pytest.mark.parametrize("release", ("native", "eventually"))
def test_concise_async_reset_rejects_other_release_contracts(
    release: str,
) -> None:
    with pytest.raises(ParseError, match="async reset release"):
        parse(
            "module Bad { clock clk async reset arst @clk { "
            f"release {release} }} }}"
        )


def test_legacy_async_block_remains_native() -> None:
    module = parse(
        "module RawAsync { clock clk reset arst @clk { "
        "mode asynchronous polarity active_high power_up unspecified } }"
    )

    reset = module.reset_physical[0]
    assert reset.release_mode == "native"
    assert reset.release_cycles == 0


def test_concise_async_reset_is_available_in_named_module_interfaces() -> None:
    module = parse(
        "interface AsyncIfc { clock clk async reset arst @clk } "
        "module Async : AsyncIfc { }"
    )

    declaration = module.module_interfaces[0]
    assert declaration.reset_physical[0].release_mode == "synchronized"
    # Interface surface inheritance is semantic normalization, not parsing.
    assert module.reset_physical == ()


@pytest.mark.parametrize("polarity", ("inverted", "sometimes"))
def test_concise_async_reset_rejects_unknown_polarity(polarity: str) -> None:
    with pytest.raises(ParseError, match="reset polarity"):
        parse(
            "module Bad { clock clk async reset arst @clk { "
            f"polarity {polarity} }} }}"
        )
