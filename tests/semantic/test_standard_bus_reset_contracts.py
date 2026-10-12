"""Exact physical-domain ownership for reusable standard-bus cores."""

from pathlib import Path

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_file, compile_source
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    ResetMode,
    ResetPolarity,
)
from zlang.parser import parse
from zlang.opt import lower
from zlang.opt.identity import canonical_ir_identity
from zlang.semantic import SemanticError


ROOT = Path(__file__).resolve().parents[2]
BUS_ROOT = ROOT / "stdlib" / "bus"

PHYSICAL_DOMAIN_OWNERS = {
    ("axi4_exclusive.zhl", "AXI4ExclusiveRamWitness"),
    ("axi4_pins.zhl", "AXI4MasterPins"),
    ("axi4_pins.zhl", "AXI4SubordinatePins"),
    ("axi4_pins.zhl", "AXI4MasterPinsWithUser"),
    ("axi4_pins.zhl", "AXI4SubordinatePinsWithUser"),
}

BRIDGES = (
    ("std.bus.apb", "APB<32,32>", "apb", "APBToRegBus<32,32>"),
    (
        "std.bus.axi_lite",
        "AXI4Lite<32,32>",
        "axi",
        "AXI4LiteToRegBus<32,32>",
    ),
    (
        "std.bus.wishbone",
        "Wishbone<32,32>",
        "wb",
        "WishboneToRegBus<32,32>",
    ),
)


def _modules(path: Path):
    parsed = parse(path.read_text())
    return (*parsed.submodules, parsed)


def _bridge_source(
    import_name: str,
    protocol: str,
    endpoint: str,
    bridge: str,
    *,
    asynchronous: bool,
) -> str:
    if asynchronous:
        physical = (
            "clock pclk { edge falling }\n"
            "async reset presetn @pclk { polarity active_low }"
        )
    else:
        physical = "clock pclk\nreset preset @pclk"
    return f"""
import std.bus.reg
import {import_name}
module BusContractTop {{
    {physical}
    interface {endpoint} : {protocol}.slave @pclk
    frontend : {bridge}
    csr : RegBusCSRTarget<32,32>
    {endpoint} -> frontend.{endpoint}
    frontend.regbus -> csr.regbus
}}
"""


def _descendants(module):
    for child in module.children:
        yield child
        yield from _descendants(child)


def test_only_physical_bus_boundaries_declare_a_fixed_domain() -> None:
    actual = {
        (path.name, module.name)
        for path in BUS_ROOT.glob("*.zhl")
        for module in _modules(path)
        if module.clocks or module.resets
    }
    assert actual == PHYSICAL_DOMAIN_OWNERS


@pytest.mark.parametrize(
    ("import_name", "protocol", "endpoint", "bridge"), BRIDGES
)
def test_register_bus_bridges_inherit_each_exact_parent_contract(
    import_name: str,
    protocol: str,
    endpoint: str,
    bridge: str,
) -> None:
    synchronous = compile_source(
        _bridge_source(
            import_name, protocol, endpoint, bridge, asynchronous=False
        ),
        top="BusContractTop",
    )
    asynchronous = compile_source(
        _bridge_source(
            import_name, protocol, endpoint, bridge, asynchronous=True
        ),
        top="BusContractTop",
    )

    sync_domain = synchronous.ir.clock_domains[0]
    async_domain = asynchronous.ir.clock_domains[0]
    assert sync_domain.clock == "pclk" and sync_domain.reset == "preset"
    assert sync_domain.reset_mode is ResetMode.SYNCHRONOUS
    assert async_domain.clock == "pclk" and async_domain.reset == "presetn"
    assert async_domain.edge is ClockEdge.FALLING
    assert async_domain.reset_mode is ResetMode.ASYNCHRONOUS
    assert async_domain.reset_polarity is ResetPolarity.ACTIVE_LOW
    assert all(
        child.clock_domains == synchronous.ir.clock_domains
        for child in _descendants(synchronous.ir)
        if child.is_sequential
    )
    assert all(
        child.clock_domains == asynchronous.ir.clock_domains
        for child in _descendants(asynchronous.ir)
        if child.is_sequential
    )
    assert synchronous.ir.elaborated_instances[0].specialization_identity != (
        asynchronous.ir.elaborated_instances[0].specialization_identity
    )
    assert {
        (component.clock_domain, component.reset_domain)
        for component in asynchronous.recursive_formal_design.components
        if component.module_name in {bridge.split("<", 1)[0], "RegBusCSRTarget"}
    } == {("pclk", "presetn")}


def test_composed_axi_core_inherits_domain_through_all_children() -> None:
    source = """
import std.bus.axi4
module AsyncAxiManagerTop {
    clock aclk { edge falling }
    async reset aresetn @aclk { polarity active_low }
    manager : AXI4ManagerWithUser<32,32,4,4,1,1,1,1,1,4>
}
"""
    result = compile_source(source, top="AsyncAxiManagerTop")
    descendants = tuple(_descendants(result.ir))
    assert {item.name for item in descendants} >= {
        "AXI4ManagerWithUser",
        "AXI4ReadManagerWithUser",
        "AXI4WriteManagerWithUser",
        "AXI4ReadManagerCore",
        "AXI4WriteManagerCore",
    }
    assert all(
        child.clock_domains == result.ir.clock_domains
        for child in descendants
        if child.is_sequential
    )
    assert {
        (component.clock_domain, component.reset_domain)
        for component in result.recursive_formal_design.components
        if component.module_name.startswith("AXI4")
    } == {("aclk", "aresetn")}


def test_explicit_root_domain_compiles_an_inherited_core_without_a_wrapper() -> None:
    source = BUS_ROOT / "axi4_subordinate.zhl"
    sync_domain = ClockDomain("clk", "rst")
    async_domain = ClockDomain(
        "hclk",
        "hresetn",
        edge=ClockEdge.FALLING,
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_polarity=ResetPolarity.ACTIVE_LOW,
    )

    with pytest.raises(SemanticError, match="requires a clock and reset"):
        compile_file(source, top="AXI4ReadSubordinate")

    synchronous = compile_file(
        source, top="AXI4ReadSubordinate", root_domain=sync_domain
    )
    asynchronous = compile_file(
        source, top="AXI4ReadSubordinate", root_domain=async_domain
    )
    assert synchronous.ir.clock_domains == (sync_domain,)
    assert asynchronous.ir.clock_domains == (async_domain,)
    assert canonical_ir_identity(lower(synchronous.ir)) != canonical_ir_identity(
        lower(asynchronous.ir)
    )
    sync_artifact = emit_artifact(synchronous.ir)
    async_artifact = emit_artifact(asynchronous.ir)
    assert sync_artifact.text == emit_artifact(synchronous.ir).text
    assert async_artifact.text == emit_artifact(asynchronous.ir).text
    assert sync_artifact.artifact_hash != async_artifact.artifact_hash
    assert "input wire logic hclk" in async_artifact.text
    assert "input wire logic hresetn" in async_artifact.text


def test_root_domain_requires_one_typed_immutable_contract() -> None:
    with pytest.raises(TypeError, match="root_domain must be a ClockDomain"):
        compile_source(
            "module Inherited { reg value:u8=0 out copy:u8=value }",
            root_domain=("clk", "rst"),  # type: ignore[arg-type]
        )


def test_inherited_bus_core_rejects_ambiguous_parent_domain() -> None:
    source = """
import std.bus.axi_lite
module Bad {
    clock a
    clock b
    reset ra @a
    reset rb @b
    frontend : AXI4LiteToRegBus<32,32>
}
"""
    with pytest.raises(
        SemanticError, match="requires a clock and reset|clock domain"
    ):
        compile_source(source, top="Bad")


def test_axi_pin_wrappers_keep_their_explicit_physical_contract() -> None:
    result = compile_file(
        BUS_ROOT / "axi4_pins.zhl", top="AXI4MasterPins"
    )
    domain = result.ir.clock_domains[0]
    assert (domain.clock, domain.reset) == ("ACLK", "ARESETn")
    assert domain.reset_mode is ResetMode.ASYNCHRONOUS
    assert domain.reset_polarity is ResetPolarity.ACTIVE_LOW
