from pathlib import Path

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.ir.cdc import ResetMode, ResetPolarity, ResetReleaseMode
from zlang.opt import OptimizationStage, lower, restore
from zlang.semantic import SemanticError


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "ahb_csr_top.zhl"


def test_ahb_lite_is_source_authored_and_canonical() -> None:
    result = compile_source(EXAMPLE.read_text(), top="AhbCsrTop")
    module = result.ir
    assert [child.name for child in module.children] == [
        "AHBLiteToRegBus",
        "AhbRegBusCSRTarget",
    ]
    bridge = module.children[0]
    domain = bridge.clock_domains[0]
    assert domain.reset_mode is ResetMode.ASYNCHRONOUS
    assert domain.reset_polarity is ResetPolarity.ACTIVE_LOW
    assert domain.reset_release_mode is ResetReleaseMode.SYNCHRONIZED
    assert domain.reset_release_cycles == 2
    assert dict(module.library_dependencies).keys() >= {
        "std.bus.ahb_lite",
        "std.bus.reg",
    }
    assert {register.name for register in bridge.registers} == {
        "address",
        "write",
        "phase",
    }
    assert {assignment.target.name for assignment in bridge.assignments} >= {
        "ahb__HRDATA",
        "ahb__HREADYOUT",
        "ahb__HRESP",
        "regbus__request",
        "regbus__response",
    }
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    assert restore(canonical) == module

    artifact = emit_artifact(module, selected_ir_identity="ahb-lite-source")
    repeated = emit_artifact(module, selected_ir_identity="ahb-lite-source")
    assert repeated.text == artifact.text
    assert repeated.artifact_hash == artifact.artifact_hash
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.physical_domains == artifact.physical_domains
    binding_ids = {binding.semantic_signal_id for binding in artifact.bindings}
    assert {
        "aggregate:AhbCsrTop.ahb.HADDR",
        "aggregate:AhbCsrTop.ahb.HWDATA",
        "aggregate:AhbCsrTop.ahb.HREADYOUT",
        "aggregate:AhbCsrTop.ahb.HRESP",
    } <= binding_ids


def test_ahb_lite_has_no_compiler_or_backend_name_dispatch() -> None:
    implementation_roots = (
        ROOT / "zlang" / "semantic",
        ROOT / "zlang" / "ir",
        ROOT / "zlang" / "opt",
        ROOT / "zlang" / "backend",
    )
    occurrences = {
        path.relative_to(ROOT): token
        for root in implementation_roots
        for path in root.rglob("*.py")
        for token in ("AHBLite", "AHB")
        if token in path.read_text()
    }
    assert occurrences == {}


@pytest.mark.parametrize("data_width", (1, 7, 24, 2048))
def test_ahb_lite_rejects_unsupported_data_widths(data_width: int) -> None:
    source = (
        "import std.bus.ahb_lite "
        "module Bad { clock hclk async reset hresetn @hclk { polarity active_low } "
        f"child : AHBLiteToRegBus<32,{data_width}> }}"
    )
    with pytest.raises(SemanticError, match="parameter constraint"):
        compile_source(source)


def test_ahb_lite_requires_enough_address_bits_for_alignment() -> None:
    source = (
        "import std.bus.ahb_lite "
        "module Bad { clock hclk async reset hresetn @hclk { polarity active_low } "
        "child : AHBLiteToRegBus<1,32> }"
    )
    with pytest.raises(SemanticError, match="parameter constraint"):
        compile_source(source)


def test_ahb_lite_inherits_exact_parent_reset_contract() -> None:
    """ZL-050: one bridge specializes to each exact enclosing contract."""

    source = """
import std.bus.ahb_lite
import std.bus.reg

module SyncTop {
    clock clk
    reset rst @clk
    interface ahb : AHBLite<32,32>.slave @clk
    interface regbus : RegBus<32,32>.requester @clk
    bridge : AHBLiteToRegBus<32,32>
    ahb -> bridge.ahb
    regbus -> bridge.regbus
}

module AsyncTop {
    clock hclk
    async reset hresetn @hclk { polarity active_low }
    interface ahb : AHBLite<32,32>.slave @hclk
    interface regbus : RegBus<32,32>.requester @hclk
    bridge : AHBLiteToRegBus<32,32>
    ahb -> bridge.ahb
    regbus -> bridge.regbus
}
"""

    synchronous_result = compile_source(source, top="SyncTop")
    asynchronous_result = compile_source(source, top="AsyncTop")
    synchronous = synchronous_result.ir
    asynchronous = asynchronous_result.ir
    sync_bridge = synchronous.children[0]
    async_bridge = asynchronous.children[0]

    assert sync_bridge.clock_domains == synchronous.clock_domains
    assert async_bridge.clock_domains == asynchronous.clock_domains
    assert synchronous_result.formal_design.clock_domains == synchronous.clock_domains
    assert asynchronous_result.formal_design.clock_domains == asynchronous.clock_domains
    assert sync_bridge.clock_domains[0].reset_mode is ResetMode.SYNCHRONOUS
    assert sync_bridge.clock_domains[0].reset_polarity is ResetPolarity.ACTIVE_HIGH
    assert sync_bridge.clock_domains[0].reset_release_mode is ResetReleaseMode.NATIVE
    assert async_bridge.clock_domains[0].reset_mode is ResetMode.ASYNCHRONOUS
    assert async_bridge.clock_domains[0].reset_polarity is ResetPolarity.ACTIVE_LOW
    assert (
        async_bridge.clock_domains[0].reset_release_mode
        is ResetReleaseMode.SYNCHRONIZED
    )
    assert synchronous.elaborated_instances[0].specialization_identity != (
        asynchronous.elaborated_instances[0].specialization_identity
    )

    sync_formal = next(
        component
        for component in synchronous_result.recursive_formal_design.components
        if component.module_name == "AHBLiteToRegBus"
    )
    async_formal = next(
        component
        for component in asynchronous_result.recursive_formal_design.components
        if component.module_name == "AHBLiteToRegBus"
    )
    assert (sync_formal.clock_domain, sync_formal.reset_domain) == ("clk", "rst")
    assert (async_formal.clock_domain, async_formal.reset_domain) == (
        "hclk",
        "hresetn",
    )


def test_ahb_lite_rejects_ambiguous_multidomain_inheritance() -> None:
    source = """
import std.bus.ahb_lite
import std.bus.reg

module Bad {
    clock a
    clock b
    reset ra @a
    reset rb @b
    interface ahb : AHBLite<32,32>.slave @a
    interface regbus : RegBus<32,32>.requester @a
    bridge : AHBLiteToRegBus<32,32>
    ahb -> bridge.ahb
    regbus -> bridge.regbus
}
"""

    with pytest.raises(
        SemanticError,
        match="requires a clock and reset|clock domain",
    ):
        compile_source(source, top="Bad")


@pytest.mark.parametrize("data_width", (8, 16, 32, 64, 1024))
def test_ahb_lite_accepts_bounded_power_of_two_bus_widths(data_width: int) -> None:
    source = f"""
import std.bus.ahb_lite
module Good {{
    clock hclk
    async reset hresetn @hclk {{ polarity active_low }}
    interface ahb : AHBLite<32,{data_width}>.slave @hclk
    interface regbus : RegBus<32,{data_width}>.requester @hclk
    bridge : AHBLiteToRegBus<32,{data_width}>
    ahb -> bridge.ahb
    regbus -> bridge.regbus
}}
"""
    module = compile_source(source).ir
    bridge = module.children[0]
    assert bridge.aggregate_protocol_endpoints[0].protocol == "AHBLite"
    assert bridge.aggregate_protocol_endpoints[1].protocol == "RegBus"
