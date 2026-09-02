from dataclasses import replace
from pathlib import Path

import pytest

from zlang.backend.systemverilog import emit_target
from zlang.backend.systemverilog.emitter import SystemVerilogEmissionError
from zlang.compiler import compile_source
from zlang.ir.target import PipelineConfiguration
from zlang.parser import parse
from zlang.stdlib import available_stdlib_modules
from zlang.targets import (
    TargetArchitectureError,
    load_target,
    validate_clock_requirement,
    validate_inventory,
    validate_memory_configuration,
    validate_pipeline_configuration,
)


def _resource(target: str, name: str):
    _, _, resources = load_target(target)
    return next(item for item in resources if item.name == name)


def test_generic_resource_family_is_complete_and_vendor_free() -> None:
    _, family, resources = load_target("generic")
    classes = {item.resource_class for item in resources}
    assert family.name == "GenericFamily"
    assert {"logic", "register", "multiplier", "adder", "dsp_mac",
            "fifo_storage", "block_memory", "clock_generator",
            "clock_control", "arithmetic_chain"} <= classes
    assert all(binding.primitive is None for item in resources for binding in item.physical_bindings)
    carry = next(item for item in resources if item.name == "GenericCarry")
    assert carry.dedicated_links[0].fabric_fallback


def test_xilinx_dsp_pipeline_sites_are_generic_and_physical_names_are_binding_data() -> None:
    dsp = _resource("xc7z030ffg676-1", "DSP48E1")
    assert tuple(item.name for item in dsp.pipeline_sites) == (
        "input_preadd", "multiply", "accumulate_output",
    )
    assert [validate_pipeline_configuration(dsp, name).latency for name in (
        "unregistered", "multiply_registered",
        "multiply_output_registered", "fully_pipelined",
    )] == [0, 1, 2, 3]
    physical = dsp.physical_bindings[0]
    assert physical.primitive == "DSP48E1"
    assert ("multiply", "MREG") in physical.pipeline_site_map
    assert ("pcascade", "PCOUT", "PCIN") in physical.dedicated_edge_map
    assert not dsp.dedicated_links[0].fabric_fallback


def test_xilinx_and_intel_memories_share_generic_legality_api() -> None:
    ramb = _resource("xc7z030ffg676-1", "RAMB36E1")
    m10k = _resource("5CSEMA5F31C6", "CycloneVM10K")
    validate_memory_configuration(ramb, width=36, depth=1024, port_mode="true_dual")
    validate_memory_configuration(m10k, width=20, depth=512, port_mode="simple_dual")
    with pytest.raises(TargetArchitectureError, match="does not support width 64"):
        validate_memory_configuration(m10k, width=64, depth=1, port_mode="single")
    with pytest.raises(TargetArchitectureError, match="capacity exceeded"):
        validate_memory_configuration(ramb, width=36, depth=2048, port_mode="single")


def test_clock_resources_use_one_generic_requirement_check() -> None:
    mmcm = _resource("xc7z030ffg676-1", "MMCME2_ADV")
    intel = _resource("5CSEMA5F31C6", "CycloneVFPLL")
    validate_clock_requirement(mmcm, input_mhz=100, output_mhz=200, outputs=2)
    validate_clock_requirement(intel, input_mhz=50, output_mhz=100, outputs=4)
    with pytest.raises(TargetArchitectureError, match="rejects input frequency"):
        validate_clock_requirement(mmcm, input_mhz=5, output_mhz=100)
    with pytest.raises(TargetArchitectureError, match="too few outputs"):
        validate_clock_requirement(intel, input_mhz=50, output_mhz=100, outputs=10)


def test_intel_resources_are_not_forced_into_xilinx_shape() -> None:
    dsp = _resource("5CSEMA5F31C6", "CycloneVVariableDSP")
    assert dsp.limit("accumulator") == 64
    assert dict(dsp.capabilities)["modes"] == "3x9x9.2x18x18.1x27x27"
    assert dsp.dedicated_links[0].name == "chain"
    assert dsp.physical_bindings[0].primitive == "cyclonev_mac"
    assert dsp.physical_bindings[0].emitter == "unsupported"


def test_asic_mac_sram_pll_and_standard_cells_use_same_ir() -> None:
    _, family, resources = load_target("generic-asic")
    assert family.name == "GenericAsicFamily"
    assert {item.resource_class for item in resources} == {
        "multiplier", "block_memory", "clock_generator", "standard_cell_logic",
    }
    assert {item.physical_bindings[0].primitive for item in resources} >= {
        "project_multiplier", "project_sram", "project_pll", None,
    }


def test_inventory_and_pipeline_configuration_fail_closed() -> None:
    target, _, resources = load_target("xc7z030ffg676-1")
    dsp = next(item for item in resources if item.name == "DSP48E1")
    validate_inventory(target, ((dsp.identity, 400),))
    with pytest.raises(TargetArchitectureError, match="inventory exceeded"):
        validate_inventory(target, ((dsp.identity, 401),))
    bad = replace(dsp, pipeline_configurations=(PipelineConfiguration(
        "bad", ("missing",), 1, 1, (),
    ),))
    with pytest.raises(TargetArchitectureError, match="unknown pipeline site"):
        validate_pipeline_configuration(bad, "bad")


def test_project_local_resource_syntax_is_not_stdlib_specific() -> None:
    unit = parse("""
resource ProjectSRAM {
  class block_memory
  operation synchronous_ram
  capability capacity_bits 8192
  pipeline_site output location memory_output latency 1 ii 1 local true delay_ps 0
  pipeline_config registered latency 1 ii 1 enable output setting macro_outreg 1
  binding systemverilog unsupported
  physical_primitive my_sram_8k
  physical_site output QREG
}
module Holder {}
""")
    declaration = unit.resource_definitions[0]
    assert declaration.name == "ProjectSRAM"
    assert declaration.pipeline_configurations[0].sites == ("output",)
    assert declaration.physical_primitive == "my_sram_8k"


def test_stdlib_discovery_is_recursive_and_contains_all_target_families() -> None:
    modules = available_stdlib_modules()
    assert "std.target.generic" in modules
    assert "std.target.xilinx.series7" in modules
    assert "std.target.intel.cyclone_v" in modules
    assert "std.target.asic.generic" in modules


def test_unsupported_intel_physical_binding_is_explicit() -> None:
    source = Path("examples/symmetric_fixed_fir.zhl").read_text(encoding="utf-8")
    selected = compile_source(
        source, target="xc7z030ffg676-1",
        architecture="Xilinx7SymmetricDSPCascade", architecture_mode="required",
    )
    intel_target, _, intel_resources = load_target("5CSEMA5F31C6")
    intel_dsp = next(item for item in intel_resources if item.name == "CycloneVVariableDSP")
    graph = replace(
        selected.implementation_graph,
        target_identity=intel_target.identity, target_hash=intel_target.source_hash,
        resources=tuple(replace(item, resource_definition_identity=intel_dsp.identity)
                        for item in selected.implementation_graph.resources),
    )
    with pytest.raises(SystemVerilogEmissionError, match="unsupported systemverilog binding"):
        emit_target(selected.ir, graph)
