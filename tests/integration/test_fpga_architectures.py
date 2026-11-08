# SPDX-License-Identifier: Apache-2.0
"""Compiler-owned Gowin/ECP5 bindings: exact arithmetic, timing and admission."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from zlang.backend.systemverilog import emit_target_artifact
from zlang.backend.systemverilog.fpga_primitives import multiplier_models
from zlang.compiler import compile_file, compile_source
from zlang.costs import CandidateCost
from zlang.formal_candidate import (
    PhysicalTargetFormalCandidate,
    SemanticEquivalenceDirectSystemVerilogCandidateVerifier,
)
from zlang.formal_exploration import FormalExplorationConfig, FormalPolicy
from zlang.formal_primitive_models import validate_primitive_models
from zlang.ir.formal import FormalStatus
from zlang.pipeline_scheduling import erase_pipeline_timing
from zlang.target_catalog import (
    TargetArchitectureError,
    load_target,
    validate_memory_configuration,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/fpga/architectures/arithmetic.zhl"
FAMILIES = (
    ("Gowin", "gw1nr_lv9qn88pc6", "MULT18X18"),
    ("Ecp5", "lfe5u_25f_8mg285c", "MULT18X18D"),
)
KINDS = (
    ("Multiply", "Multiply"),
    ("UnsignedMultiply", "Multiply"),
    ("MultiplyAdd", "MultiplyAddFabric"),
    ("PreaddMultiply", "MultiplyAddFabric"),
    ("SignedReduction", "SignedProductFabric"),
    ("Reduction4", "SignedProductFabric4"),
    ("SymmetricFirAuto", "SymmetricFirFabric"),
)


def selected(family, kind, latency):
    prefix, target, _ = family
    suffix = (
        ("", "Delay2", "Delay3")
        if kind == "SymmetricFirAuto"
        else ("", "MREG", "MREGOREG")
    )[latency - 1]
    architecture = dict(KINDS)[kind] + suffix
    return compile_file(
        SOURCE,
        top=kind + str(latency),
        target=target,
        architecture=prefix + architecture,
        architecture_mode="required",
    )


@pytest.fixture(scope="module")
def arithmetic_products():
    return {
        (f[0], kind, n): selected(f, kind, n)
        for f in FAMILIES
        for kind, _ in KINDS
        for n in (1, 2, 3)
    }


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("kind", tuple(k for k, _ in KINDS))
@pytest.mark.parametrize("latency", (1, 2, 3))
def test_arithmetic_actual_rtl_is_proven(
    arithmetic_products, family, kind, latency, tmp_path
):
    result = arithmetic_products[family[0], kind, latency]
    graph = result.implementation_graph
    value = result.ir.assignments[0].expression
    candidate = PhysicalTargetFormalCandidate(
        value,
        result.ir,
        graph,
        graph.semantic_region_identity,
        graph.identity,
        CandidateCost.estimate(lut=0, dsp=len(graph.resources), latency=graph.latency),
    )
    verifier = SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        erase_pipeline_timing(value), clock_domain_contract=result.ir.clock_domains[0]
    )
    config = FormalExplorationConfig(
        policy=FormalPolicy.REQUIRED_PROVEN,
        bmc_depth=8,
        timeout_seconds=30,
        work_directory=tmp_path,
    )
    before = emit_target_artifact(result.ir, graph)
    proof = verifier(candidate, config)
    (tmp_path / "evidence.json").write_text(
        json.dumps(proof, default=str, indent=2) + "\n"
    )
    assert proof["status"] is FormalStatus.PROVEN, proof["reason"]
    assert emit_target_artifact(result.ir, graph) == before
    assert not graph.dedicated_edges
    assert before.implementation.emitted_resource_counts == (
        (family[2], len(graph.resources)),
    )
    assert sum(
        count for _, count in before.implementation.intended_resource_counts
    ) == len(graph.resources)
    assert all(
        dict(n.configuration)["reserved_macro_units"] == 1 for n in graph.resources
    )


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("count", (1, 2, 3, 4, 5, 6, 7, 8))
def test_parallel_products_exact_bound_and_complete_output(family, count, tmp_path):
    prefix, target, _ = family
    r = compile_file(
        SOURCE,
        top=f"Parallel{count}",
        target=target,
        architecture=f"{prefix}ParallelProducts{count}",
        architecture_mode="required",
    )
    g = r.implementation_graph
    value = r.ir.assignments[0].expression
    assert len(g.resources) == count and g.latency == 1 and not g.dedicated_edges
    c = PhysicalTargetFormalCandidate(
        value,
        r.ir,
        g,
        g.semantic_region_identity,
        g.identity,
        CandidateCost.estimate(lut=0, dsp=count, latency=1),
    )
    ev = SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        erase_pipeline_timing(value), clock_domain_contract=r.ir.clock_domains[0]
    )(
        c,
        FormalExplorationConfig(
            policy=FormalPolicy.REQUIRED_PROVEN,
            bmc_depth=8,
            timeout_seconds=30,
            work_directory=tmp_path,
        ),
    )
    assert ev["status"] is FormalStatus.PROVEN, ev


@pytest.mark.parametrize("family", FAMILIES)
def test_preadder_does_not_narrow_and_preferred_falls_back(family):
    pref, target, _ = family
    source = "module Top { clock clk reset rst in a,d,b:s18 out y:s37 y=pipeline(2){(a+d)*b} }"
    with pytest.raises(TargetArchitectureError, match="no legal candidate"):
        compile_source(
            source,
            target=target,
            architecture=pref + "MultiplyAddFabricMREG",
            architecture_mode="required",
        )
    assert compile_source(
        source,
        target=target,
        architecture=pref + "MultiplyAddFabricMREG",
        architecture_mode="preferred",
    ).implementation_graph.is_generic


@pytest.mark.parametrize("family", FAMILIES)
def test_shared_macro_pool_has_no_double_allocation(family):
    _, target, _ = family
    device, _, resources = load_target(target)
    macro = next(
        r
        for r in resources
        if dict(r.capabilities).get("inventory_unit") == "whole_macro"
    )
    from zlang.target_catalog import validate_inventory

    capacity = dict(device.inventory)[macro.identity]
    validate_inventory(device, ((macro.identity, capacity),))
    with pytest.raises(TargetArchitectureError):
        validate_inventory(device, ((macro.identity, capacity + 1),))
    assert dict(macro.capabilities)["hard_multipliers_per_macro"] == "2"


@pytest.mark.parametrize("family", FAMILIES)
def test_joint_bram_geometry_and_collision_rejects_unsupported(family):
    _, target, _ = family
    _, _, resources = load_target(target)
    ram = next(r for r in resources if r.resource_class == "block_memory")
    validate_memory_configuration(ram, width=18, depth=1024, port_mode="simple_dual")
    with pytest.raises(TargetArchitectureError):
        validate_memory_configuration(
            ram, width=36, depth=1024, port_mode="simple_dual"
        )
    with pytest.raises(TargetArchitectureError):
        validate_memory_configuration(ram, width=36, depth=512, port_mode="true_dual")


@pytest.mark.parametrize("family", FAMILIES)
def test_new_models_reject_self_consistent_stale_text(family):
    prefix, _, primitive = family
    text = multiplier_models("gowin" if prefix == "Gowin" else "ecp5")
    validate_primitive_models(SimpleNamespace(text=text))
    with pytest.raises(ValueError, match="stale " + primitive):
        validate_primitive_models(SimpleNamespace(text=text.replace("p=mid", "p=~mid")))
    with pytest.raises(ValueError, match="stale " + primitive):
        validate_primitive_models(
            SimpleNamespace(text=f"module user; {primitive} dut(); endmodule")
        )


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    "mutation", ("native_asign", "native_product_pipeline", "realization")
)
def test_graph_binding_mutations_fail_closed(arithmetic_products, family, mutation):
    result = arithmetic_products[family[0], "Multiply", 2]
    g = result.implementation_graph
    n = g.resources[0]
    cfg = dict(n.configuration)
    cfg[mutation] = 0 if mutation == "native_asign" else "corrupt"
    broken = replace(g, resources=(replace(n, configuration=tuple(cfg.items())),))
    with pytest.raises((ValueError, TypeError)):
        emit_target_artifact(result.ir, broken)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    "mutation", ("sign", "operand", "reset", "pipeline", "enable", "accumulator")
)
def test_actual_rtl_mutations_are_counterexamples(
    family, mutation, tmp_path, monkeypatch
):
    import zlang.backend.systemverilog.fpga_dsp as renderer

    result = selected(family, "MultiplyAdd", 2)
    g = result.implementation_graph
    emit = renderer.emit_fpga_dsp

    def corrupt(*args, **kwargs):
        text = emit(*args, **kwargs)
        if mutation == "sign":
            before = ".ASIGN(1'b1)" if family[0] == "Gowin" else ".SIGNEDA(1'b1)"
            after = before.replace("1'b1", "1'b0")
        elif mutation == "operand":
            before = "assign zlang_fpga_b0 = 18'($signed(b));"
            after = "assign zlang_fpga_b0 = 18'($signed(a));"
        elif mutation == "reset":
            before = "if (rst) begin"
            after = "if (!rst) begin"
        elif mutation == "enable":
            before = ".CE(1'b1)" if family[0] == "Gowin" else ".CE0(1'b1)"
            after = before.replace("1'b1", "1'b0")
        elif mutation == "pipeline":
            before = (
                ".PIPE_REG(1)" if family[0] == "Gowin" else '.REG_PIPELINE_CLK("CLK0")'
            )
            after = (
                ".PIPE_REG(0)" if family[0] == "Gowin" else '.REG_PIPELINE_CLK("NONE")'
            )
        else:
            before = " + 54'($signed(zlang_fpga_product0[35:0]))"
            after = " - 54'($signed(zlang_fpga_product0[35:0]))"
        assert before in text, (mutation, before)
        return text.replace(before, after, 1)

    monkeypatch.setattr(renderer, "emit_fpga_dsp", corrupt)
    value = result.ir.assignments[0].expression
    c = PhysicalTargetFormalCandidate(
        value,
        result.ir,
        g,
        g.semantic_region_identity,
        g.identity,
        CandidateCost.estimate(lut=0, dsp=1, latency=2),
    )
    proof = SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        erase_pipeline_timing(value), clock_domain_contract=result.ir.clock_domains[0]
    )(
        c,
        FormalExplorationConfig(
            policy=FormalPolicy.REQUIRED_PROVEN,
            bmc_depth=8,
            timeout_seconds=30,
            work_directory=tmp_path,
        ),
    )
    assert proof["status"] is FormalStatus.FAILED, proof


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    "kind",
    (
        "Multiply",
        "UnsignedMultiply",
        "MultiplyAdd",
        "PreaddMultiply",
        "SignedReduction",
    ),
)
def test_native_matches_selected_rtl_through_reset_bubbles_and_extrema(
    family, kind, tmp_path, monkeypatch
):
    from zlang import sim
    import zlang.simulation_compare as comparison

    result = selected(family, kind, 2)
    artifact = emit_target_artifact(
        result.ir, result.implementation_graph, simulation_model=True
    )
    # Only replace the RTL leg; native executes the same public source through
    # the ordinary source-matched ABI-v13 plan and immutable event protocol.
    monkeypatch.setattr(comparison, "emit_artifact", lambda *a, **kw: artifact)
    program = sim.compile(SOURCE, top=kind + "2")
    inputs = {p.name: p.type for p in result.ir.inputs}
    vectors = []
    for index in range(16):
        values = {}
        for j, (name, ty) in enumerate(inputs.items()):
            signed = type(ty).__name__ in {"SIntType", "FixedType"}
            extrema = (
                0,
                1,
                ((1 << (ty.width - 1)) - 1) if signed else (1 << ty.width) - 1,
                -(1 << (ty.width - 1)) if signed else 0,
            )
            values[name] = extrema[(index + j) % len(extrema)]
        vectors.append(
            {"set": values, "reset": {"rst": index in (0, 7, 8)}, "edges": ["clk"]}
            if index == 0
            else {"set": values, "reset": {"rst": index in (0, 7, 8)}}
        )
        if index:
            vectors.append({"edges": ["clk"]})
    observed = comparison.compare_program(
        program,
        vectors,
        simulator="iverilog",
        artifact_directory=tmp_path / "comparison",
        timeout=120,
    )
    assert observed.native_trace == observed.rtl_trace


@pytest.mark.parametrize("family", FAMILIES)
def test_unproven_native_fir_pipeline_stays_unavailable(family):
    from zlang.target_catalog import load_architecture
    from zlang.target_mapping_fir import map_manual_architecture

    prefix, target, _ = family
    module = compile_file(SOURCE, top="SymmetricFir2").ir
    device, domain, resources = load_target(target)
    template = replace(
        load_architecture(prefix + "SymmetricFirFabricDelay2"),
        pipeline_configuration="multiply_registered",
    )
    with pytest.raises(TargetArchitectureError, match="not qualified"):
        map_manual_architecture(module, device, domain, resources, template)


@pytest.mark.parametrize(
    "width,fraction,out_width,out_fraction",
    ((7, 4, 4, 2), (27, 20, 16, 14), (31, 24, 16, 14), (5, 3, 4, 2)),
)
def test_fir_quotient_rounding_is_universally_equivalent(
    width, fraction, out_width, out_fraction, tmp_path
):
    from zlang.backend.systemverilog.fpga_dsp import _fir_round_sat
    from zlang.backend.systemverilog import emit_artifact
    from zlang.formal import run_verilog_formal
    from zlang.ir.formal import ProofMode

    reference = compile_source(f"""module Reference {{ in x:fixed<{width},{fraction}> out y:fixed<{out_width},{out_fraction}>
y=quantize<fixed<{out_width},{out_fraction}>>(x) {{ round nearest_even overflow saturate }} }}""")
    value = reference.ir.assignments[0].expression
    lines = []
    rhs = _fir_round_sat(
        value, value.expression, lambda preferred, identity: preferred, lines
    )
    source = (
        emit_artifact(reference.ir).text
        + f"\nmodule top(input [{width - 1}:0] x);\nwire [{out_width - 1}:0] ref_y;Reference oracle(.x,.y(ref_y));\n"
        + "\n".join(lines)
        + f"\nwire [{out_width - 1}:0] actual_y={rhs};\nalways @* assert(ref_y==actual_y);\nendmodule\n"
    )
    proof = run_verilog_formal(
        source,
        top="top",
        property_id="rounding",
        mode=ProofMode.PROVE,
        depth=8,
        timeout_seconds=30,
        systemverilog=True,
        flatten=True,
        work_directory=tmp_path,
    )
    assert proof.status is FormalStatus.PROVEN, proof
