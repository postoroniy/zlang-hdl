from dataclasses import replace
import json
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact, IMPLEMENTATION_MANIFEST_VERSION
from zlang.backend.systemverilog import emit_experimental, emit_target, emit_target_artifact
from zlang.backend.systemverilog.emitter import SystemVerilogEmissionError
from zlang.compiler import compile_source
from zlang.fixed_point import quantize_rational
from zlang.ir import expressions as expr
from zlang.targets import (
    TargetArchitectureError,
    load_architecture,
    load_target,
    map_manual_architecture,
    select_implementation_graph,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "examples/symmetric_fixed_fir.zhl").read_text()
TARGET = "xc7z030ffg676-1"
ARCHITECTURE = "Xilinx7SymmetricDSPCascade"


def _selected(source: str = SOURCE):
    return compile_source(
        source, target=TARGET, architecture=ARCHITECTURE,
        architecture_mode="required",
    )


def test_source_defined_target_resource_and_architecture_load() -> None:
    target, family, resources = load_target(TARGET)
    dsp = next(item for item in resources if item.name == "DSP48E1")
    architecture = load_architecture(ARCHITECTURE)
    assert target.part == TARGET
    assert dict(target.inventory)[dsp.identity] == 400
    assert family.name == "Xilinx7Series"
    assert dsp.limit("preadder") == 25
    assert dsp.limit("multiplier_b") == 18
    assert dsp.limit("accumulator") == 48
    assert dsp.dedicated_links[0].width == 48
    assert architecture.pipeline_configuration == "unregistered"
    assert architecture.register_configuration == ()
    assert [item.name for item in dsp.pipeline_sites] == [
        "input_preadd", "multiply", "accumulate_output",
    ]
    assert dsp.pipeline_configuration("fully_pipelined").latency == 3
    physical = dsp.physical_bindings[0]
    assert physical.primitive == "DSP48E1"
    assert ("multiply", "MREG") in physical.pipeline_site_map


def test_toy_asic_uses_the_same_vendor_neutral_ir() -> None:
    target, family, resources = load_target("toy-asic")
    resource = resources[0]
    architecture = load_architecture("ToyMACArchitecture")
    assert target.name == "toy_asic"
    assert family.name == "ToyAsicFamily"
    assert resource.name == "ToyMAC"
    assert not resource.dedicated_links
    assert architecture.resource_name == "ToyMAC"




def test_symmetric_shape_maps_to_four_nodes_and_three_real_edges() -> None:
    graph = _selected().implementation_graph
    assert len(graph.resources) == 4
    assert len(graph.dedicated_edges) == 3
    assert (graph.latency, graph.initiation_interval) == (1, 1)
    assert [dict(item.configuration)["sample_left_index"] for item in graph.resources] == [0, 1, 2, 3]
    assert [dict(item.configuration)["sample_right_index"] for item in graph.resources] == [7, 6, 5, 4]
    assert [dict(item.configuration)["coefficient_index"] for item in graph.resources] == [0, 1, 2, 3]
    assert all(edge.kind == "pcascade" and edge.width == 48 for edge in graph.dedicated_edges)
    assert all(edge.placement_relation == "adjacent_same_column" for edge in graph.dedicated_edges)
    assert graph.pipeline_configuration_identity.endswith(".unregistered")
    assert graph.active_pipeline_sites == ()
    assert graph.physical_binding_identities
    assert "one final FixedConvert remains outside the resource cascade" in graph.legality_evidence


def test_symmetry_is_semantic_not_a_module_or_vector_value_guess() -> None:
    independent = SOURCE.replace("vec<4,SF2.10>", "vec<8,SF2.10>")
    independent = independent.replace("samples[7] * coefficients[0]", "samples[7] * coefficients[7]")
    independent = independent.replace("samples[6] * coefficients[1]", "samples[6] * coefficients[6]")
    independent = independent.replace("samples[5] * coefficients[2]", "samples[5] * coefficients[5]")
    independent = independent.replace("samples[4] * coefficients[3]", "samples[4] * coefficients[4]")
    with pytest.raises(TargetArchitectureError, match="four semantically reused coefficients"):
        compile_source(
            independent, target=TARGET, architecture=ARCHITECTURE,
            architecture_mode="required",
        )


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (SOURCE.replace("vec<8,SF2.10>", "vec<8,fixed<25,10>>"),
         r"preadder width exceeded.*required width 26.*supports 25"),
        (SOURCE.replace("vec<4,SF2.10>", "vec<4,fixed<19,10>>"),
         r"multiplier input B width exceeded.*required width 19.*supports 18"),
    ),
)
def test_illegal_exact_width_is_rejected_but_generic_still_compiles(source: str, message: str) -> None:
    assert compile_source(source).implementation_graph.is_generic
    with pytest.raises(TargetArchitectureError, match=message):
        compile_source(
            source, target=TARGET, architecture=ARCHITECTURE,
            architecture_mode="required",
        )


def test_accumulator_width_and_resource_inventory_are_checked_generically() -> None:
    module = compile_source(SOURCE).ir
    target, family, resources = load_target(TARGET)
    template = load_architecture(ARCHITECTURE)
    dsp = resources[0]
    narrow = replace(
        dsp,
        limits=tuple((name, 26 if name == "accumulator" else value)
                     for name, value in dsp.limits),
    )
    with pytest.raises(TargetArchitectureError, match=r"accumulator width exceeded.*required width 27.*supports 26"):
        map_manual_architecture(module, target, family, (narrow,), template)
    insufficient = replace(target, inventory=((dsp.identity, 3),))
    with pytest.raises(TargetArchitectureError, match=r"inventory exceeded.*requires 4.*provides 3"):
        map_manual_architecture(module, insufficient, family, resources, template)
    unavailable = replace(template, resource_name="MissingMAC")
    with pytest.raises(TargetArchitectureError, match=r"requires unavailable resource 'MissingMAC'"):
        map_manual_architecture(module, target, family, resources, unavailable)
    no_cascade = replace(dsp, dedicated_links=())
    with pytest.raises(TargetArchitectureError, match=r"requires unavailable dedicated connection 'pcascade'"):
        map_manual_architecture(module, target, family, (no_cascade,), template)


def test_unknown_required_and_preferred_selection_diagnostics() -> None:
    module = compile_source(SOURCE).ir
    with pytest.raises(TargetArchitectureError, match="unknown target"):
        select_implementation_graph(module, target="not-a-part")
    with pytest.raises(TargetArchitectureError, match="unknown architecture"):
        select_implementation_graph(module, target=TARGET, architecture="Missing", mode="required")
    with pytest.raises(TargetArchitectureError, match="required architecture is unavailable"):
        select_implementation_graph(module, target=TARGET, mode="required")
    fallback = select_implementation_graph(
        module, target=TARGET, architecture="ToyMACArchitecture", mode="preferred"
    )
    assert fallback.is_generic
    assert fallback.selection_policy == "preferred"
    assert any("preferred architecture rejected" in item for item in fallback.legality_evidence)
    explicitly_generic = select_implementation_graph(
        module, target=TARGET, architecture=ARCHITECTURE, mode="generic"
    )
    assert explicitly_generic.is_generic


def test_direct_sv_consumes_graph_and_manifest_round_trips() -> None:
    result = _selected()
    rtl = emit_target(result.ir, result.implementation_graph, simulation_model=True)
    assert rtl.count(") dsp0_primitive (") == 1
    assert rtl.count(") dsp1_primitive (") == 1
    assert rtl.count(") dsp2_primitive (") == 1
    assert rtl.count(") dsp3_primitive (") == 1
    assert ".PCIN(dsp0_pcout)" in rtl
    assert ".PCIN(dsp1_pcout)" in rtl
    assert ".PCIN(dsp2_pcout)" in rtl
    assert rtl.count("assign dsp_acc = dsp3_p;") == 1
    assert rtl.count("nearest_even") == 0  # lowering is logic, not an RTL annotation
    artifact = emit_target_artifact(result.ir, result.implementation_graph)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.manifest_version == IMPLEMENTATION_MANIFEST_VERSION
    assert restored.implementation == artifact.implementation
    manifest = json.loads(artifact.to_json())["implementation"]
    assert len(manifest["resources"]) == 4
    assert len(manifest["dedicated_edges"]) == 3
    assert manifest["intended_resource_counts"][0][1] == 4
    assert manifest["emitted_resource_counts"] == manifest["intended_resource_counts"]
    assert manifest["pipeline_configuration_identity"].endswith(".unregistered")
    assert manifest["physical_binding_identities"]


def test_backend_rejects_an_unimplemented_source_resource_binding() -> None:
    result = _selected()
    graph = replace(
        result.implementation_graph,
        target_identity="std.target.toy_asic.toy_asic",
        target_hash=load_target("toy-asic")[0].source_hash,
        resources=tuple(replace(item, resource_definition_identity="std.target.toy_asic.ToyMAC")
                        for item in result.implementation_graph.resources),
    )
    with pytest.raises(SystemVerilogEmissionError, match="unsupported systemverilog binding"):
        emit_target(result.ir, graph)


def test_backend_rejects_an_illegal_dedicated_edge() -> None:
    result = _selected()
    first = replace(result.implementation_graph.dedicated_edges[0], width=47)
    graph = replace(
        result.implementation_graph,
        dedicated_edges=(first, *result.implementation_graph.dedicated_edges[1:]),
    )
    with pytest.raises(SystemVerilogEmissionError, match=r"illegal dedicated edge.*48-bit PCOUT"):
        emit_target(result.ir, graph)


def _oracle(samples: tuple[int, ...], coefficients: tuple[int, ...]) -> int:
    expanded = (
        coefficients[0], coefficients[1], coefficients[2], coefficients[3],
        coefficients[3], coefficients[2], coefficients[1], coefficients[0],
    )
    return quantize_rational(
        sum(a * b for a, b in zip(samples, expanded, strict=True)),
        1 << 6, fraction=0, width=16, signed=True,
        rounding=expr.FixedRounding.NEAREST_EVEN,
        overflow=expr.FixedOverflow.SATURATE,
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_target_dsp_model_is_bit_exact_in_verilator(tmp_path: Path) -> None:
    result = _selected()
    rtl = tmp_path / "target.sv"
    target_text = emit_target(
        result.ir, result.implementation_graph, simulation_model=True
    )
    assert "input wire logic signed [11:0] samples [0:7]" in target_text
    assert "input wire logic signed [11:0] coefficients [0:3]" in target_text
    assert "module SymmetricFixedFIR_zlang_core (" in target_text
    target_artifact = emit_target_artifact(
        result.ir, result.implementation_graph, simulation_model=True
    )
    target_bindings = {
        binding.semantic_signal_id: binding
        for binding in target_artifact.bindings
    }
    assert target_bindings["port:samples"].rtl_path == "samples"
    assert target_bindings["port:samples"].physical_available
    assert target_bindings["port:coefficients"].rtl_path == "coefficients"
    assert target_bindings["port:coefficients"].physical_available
    rtl.write_text(target_text)
    generic_rtl = tmp_path / "generic.sv"
    generic_rtl.write_text(
        emit_experimental(compile_source(SOURCE).ir).replace(
            "module SymmetricFixedFIR (", "module SymmetricFixedFIRGeneric (", 1
        )
    )
    vectors = (
        ((0,) * 8, (0,) * 4),
        ((1024,) * 8, (1024,) * 4),
        ((-1024, 512, -256, 128, 64, -32, 16, -8), (256, -128, 64, -32)),
        ((2047,) * 8, (2047,) * 4),
        ((-2048,) * 8, (2047,) * 4),
    )

    def drive(name, values):
        return "".join(
            f"{name}[{index}]=12'h{value & 0xfff:03x};"
            for index, value in enumerate(values)
        )

    checks = []
    for index, (samples, coefficients) in enumerate(vectors):
        expected = _oracle(samples, coefficients)
        literal = f"-16'sd{-expected}" if expected < 0 else f"16'sd{expected}"
        checks.append(
            f"{drive('samples', samples)} {drive('coefficients', coefficients)} tick; "
            f"if ($signed(result) !== {literal}) $fatal(1,\"target vector {index}: %0d\",$signed(result)); "
            f"if ($signed(generic_result) !== {literal}) $fatal(1,\"generic vector {index}: %0d\",$signed(generic_result));"
        )
    bench = tmp_path / "tb.sv"
    bench.write_text(
        "module tb; logic clk=0,rst=1; logic signed [11:0] samples[0:7]; "
        "logic signed [11:0] coefficients[0:3]; "
        "wire signed [15:0] result,generic_result; "
        "SymmetricFixedFIR dut(.clk,.rst,.samples,.coefficients,.result); "
        "SymmetricFixedFIRGeneric generic_dut(.clk,.rst,.samples,.coefficients,.result(generic_result)); "
        "task tick; begin #1 clk=1; #1; clk=0; #1; end endtask "
        f"initial begin tick; rst=0; {''.join(checks)} $finish; end endmodule\n"
    )
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        ("verilator", "--binary", "--timing", "-Wno-fatal", "--top-module", "tb",
         str(rtl), str(generic_rtl), str(bench), "-Mdir", str(obj)),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run((str(obj / "Vtb"),), check=True, capture_output=True, text=True)
