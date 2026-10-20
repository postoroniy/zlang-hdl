"""Actual RTL, physical models and warm evidence retain independent checks."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.semantic.test_bilinear_intent import source
from tests.semantic.test_candidate_equivalence_orchestration import _prepared
import zlang.candidate_equivalence as execution
from zlang.backend.systemverilog.target import _dsp48e1_simulation_model
from zlang.compiler import compile_source
from zlang.candidate_sites import selected_candidate_sites
from zlang.equivalence import artifact_hash
from zlang.equivalence_replay_integrity import validate_prepared_equivalence_source
from zlang.formal import FormalToolchainContext
from zlang.formal_rtl_arithmetic import run_rtl_arithmetic_formal
from zlang.formal_primitive_models import validate_primitive_models
from zlang.formal_artifact_provider import FormalArtifactProvider
from zlang.formal_candidate import (
    FormalCandidateUnavailable,
    PhysicalTargetFormalCandidate,
    SemanticEquivalenceDirectSystemVerilogCandidateVerifier,
)
from zlang.formal_exploration import (
    FormalExplorationConfig, FormalExplorationError, FormalPolicy, gate_candidates,
)
from zlang.ir.equivalence import EquivalenceMode, EquivalenceStatus
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.ir import expressions as e


def test_result_cache_checks_current_harness_before_warm_reuse(tmp_path, monkeypatch):
    prepared = _prepared()
    config = FormalExplorationConfig(policy=FormalPolicy.REQUIRED_PROVEN, bmc_depth=2,
        timeout_seconds=20, work_directory=tmp_path / "work")
    tools = FormalToolchainContext.discover(engine="sby", solver="z3")
    provider = FormalArtifactProvider(tmp_path / "cache")
    result = execution._execute_semantic_equivalence(prepared, config, EquivalenceMode.PROVE, provider, tools)
    assert result.status is EquivalenceStatus.PROVEN, result.reason
    monkeypatch.setattr(execution, "run_equivalence_formal", lambda *_a, **_k: pytest.fail("exact warm cache missed"))
    assert execution._execute_semantic_equivalence(prepared, config, EquivalenceMode.PROVE, provider, tools) == result
    provider = FormalArtifactProvider(tmp_path / "cache")
    assert execution._execute_semantic_equivalence(prepared, config, EquivalenceMode.PROVE, provider, tools) == result
    assert provider.stats.disk_hits == 1
    before = provider.stats
    bad = replace(prepared, source=prepared.source.replace("assert", "assume"))
    with pytest.raises(ValueError, match="replay identity"):
        execution._execute_semantic_equivalence(bad, config, EquivalenceMode.PROVE, provider, tools)
    assert provider.stats == before


def test_ordinary_dsp_named_port_is_not_a_primitive_dependency():
    compiled = compile_source("""
module NamedPort {
    in DSP48E1:u8
    out y:u8
    y = implement { DSP48E1 ^ 0 intent { minimize lut } }
}
""")
    site = selected_candidate_sites(compiled.ir, compiled.exploration_results)[0]
    verifier = SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        site.reference_expression, candidate_class=site.candidate_class,
    )
    bundle = verifier.prepare(site.candidate, FormalExplorationConfig())
    validate_prepared_equivalence_source(bundle)


@pytest.mark.parametrize("instance", ("DSP48E1 #(.AREG(0)) dut();", "DSP48E1 dut();"))
def test_missing_primitive_model_is_rejected(instance):
    with pytest.raises(ValueError, match="stale DSP48E1"):
        validate_primitive_models(SimpleNamespace(text="module user; " + instance + " endmodule"))


@pytest.fixture(scope="module")
def physical():
    compiled = compile_source(source(width=2), target="xc7z030ffg676-1")
    selected = compiled.exploration_results[0].selected
    candidate = PhysicalTargetFormalCandidate(
        compiled.ir.assignments[0].expression, compiled.ir, compiled.implementation_graph,
        selected.semantic_identity, compiled.implementation_graph.identity, selected.cost,
    )
    verifier = SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        compiled.exploration_results[0].request.root,
        clock_domain_contract=compiled.ir.clock_domains[0],
    )
    return candidate, verifier


def test_warm_preparation_rejects_changed_actual_output(physical):
    candidate, verifier = physical
    config = FormalExplorationConfig()
    prepared = verifier.prepare(candidate, config)
    value = candidate.expression.expression
    a, b = value.fields
    wrong = replace(candidate.expression, expression=replace(value, fields=((a[0], b[1]), (b[0], a[1]))))
    module = replace(candidate.module, assignments=(replace(candidate.module.assignments[0], expression=wrong),))
    with pytest.raises(FormalCandidateUnavailable, match="actual output assignment"):
        verifier.prepare(replace(candidate, module=module), config)
    assert verifier.prepare(candidate, config) == prepared


def test_rehashed_stale_model_is_not_accepted(physical):
    candidate, verifier = physical
    bundle = verifier.prepare(candidate, FormalExplorationConfig())
    original = bundle.implementation_artifact
    text = original.text.replace("$signed(b_value)", "$signed(b_value[ZLANG_B_WIDTH-1:0])")
    assert text != original.text
    digest = artifact_hash(text)
    artifact = replace(original, text=text, artifact_hash=digest,
        bindings=tuple(replace(binding, artifact_hash=digest) for binding in original.bindings),
        implementation=replace(original.implementation, implementation_artifact_hash=digest))
    altered = replace(bundle, implementation_artifact=artifact, implementation_artifact_hash=digest,
                      source=bundle.source.replace(original.text, text))
    with pytest.raises(ValueError, match="stale DSP48E1"):
        validate_prepared_equivalence_source(altered)


def test_warm_preparation_cannot_reuse_stale_operand_identity(physical, tmp_path):
    candidate, verifier = physical
    config = FormalExplorationConfig(policy=FormalPolicy.REQUIRED_PROVEN, bmc_depth=3,
        timeout_seconds=20, work_directory=tmp_path)
    before = verifier.prepare(candidate, config)
    recipe = verifier.preparation_cache_recipe(candidate, config)
    graph = candidate.implementation_graph
    resource = graph.resources[0]
    changed = replace(resource, semantic_mappings=tuple(
        replace(mapping, expression=e.Constant(0, mapping.expression.type))
        if mapping.resource_port == "b" else mapping
        for mapping in resource.semantic_mappings
    ))
    altered = replace(candidate, implementation_graph=replace(graph, resources=(changed, *graph.resources[1:])))
    assert changed.identity == resource.identity
    assert tuple(item.semantic_identity for item in changed.semantic_mappings) == tuple(
        item.semantic_identity for item in resource.semantic_mappings
    )
    assert verifier.preparation_cache_recipe(altered, config) != recipe
    with pytest.raises(FormalCandidateUnavailable, match="physical multiplier operands differ"):
        verifier.prepare(altered, config)
    evidence = verifier(altered, config)
    assert evidence["status"] is FormalStatus.SKIPPED
    assert "physical multiplier operands differ" in evidence["reason"]
    # Invalid bindings reject before emission, not as a fabricated RTL proof.
    ranked = SimpleNamespace(candidate=altered, legal=True, objective_key=(0,))
    with pytest.raises(FormalExplorationError, match="inconclusive"):
        gate_candidates((altered,), (ranked,), config, verifier)
    assert verifier.prepare(candidate, config) == before


@pytest.mark.parametrize("mode", ("multiply", "preadd", "presubtract", "d_only", "accumulate", "subtract_product", "subtract_pcin"))
def test_dsp_model_uses_physical_ports_not_width_hints(mode, tmp_path):
    use_d = "TRUE" if mode in ("preadd", "presubtract", "d_only") else "FALSE"
    inmode = {"preadd": 4, "presubtract": 12, "d_only": 6}.get(mode, 0)
    expected = {"preadd": "a+d", "presubtract": "d-a", "d_only": "d"}.get(mode, "a")
    accumulate = mode in ("accumulate", "subtract_product", "subtract_pcin")
    opmode = "0010101" if accumulate else "0000101"
    alumode = {"subtract_product": "0011", "subtract_pcin": "0001"}.get(mode, "0000")
    carry = int(mode == "subtract_pcin")
    result_value = {
        "accumulate": "$signed(pcin) + 48'($signed(product))",
        "subtract_product": "$signed(pcin) - 48'($signed(product))",
        "subtract_pcin": "48'($signed(product)) - $signed(pcin)",
    }.get(mode, "48'($signed(product))")
    rtl = _dsp48e1_simulation_model() + f'''
module physical_signature(input wire signed [24:0] a, d, input wire signed [17:0] b,
 input wire signed [47:0] pcin);
wire [47:0] p;
wire signed [24:0] pre = {expected};
wire signed [42:0] product = pre*b;
DSP48E1 #(.USE_DPORT("{use_d}"), .ZLANG_A_WIDTH(1), .ZLANG_D_WIDTH(1),
 .ZLANG_B_WIDTH(1), .ZLANG_PRE_WIDTH(1), .ZLANG_ACC_WIDTH(2)) dut(
 .A({{{{5{{a[24]}}}},a}}), .B(b), .D(d), .C(48'd0), .PCIN(pcin),
 .ACIN(30'd0), .BCIN(18'd0), .CARRYCASCIN(1'b0), .MULTSIGNIN(1'b0),
 .ALUMODE(4'b{alumode}), .CARRYIN(1'b{carry}), .CARRYINSEL(3'd0), .OPMODE(7'b{opmode}),
 .INMODE(5'd{inmode}), .CLK(1'b0),
 .CEA1(1'b1), .CEA2(1'b1), .CEAD(1'b1), .CEALUMODE(1'b1),
 .CEB1(1'b1), .CEB2(1'b1), .CEC(1'b1), .CECARRYIN(1'b1), .CECTRL(1'b1),
 .CED(1'b1), .CEINMODE(1'b1), .CEM(1'b1), .CEP(1'b1),
 .RSTA(1'b0), .RSTB(1'b0), .RSTC(1'b0), .RSTD(1'b0), .RSTM(1'b0), .RSTP(1'b0),
 .RSTCTRL(1'b0), .RSTINMODE(1'b0), .RSTALUMODE(1'b0), .RSTALLCARRYIN(1'b0), .P(p));
always @* assert(p == ({result_value}));
endmodule
'''
    result = run_rtl_arithmetic_formal(rtl, top="physical_signature", property_id="physical-ports",
        mode=ProofMode.PROVE, depth=2, timeout_seconds=20, work_directory=tmp_path, systemverilog=True, solver="z3")
    assert result.status is FormalStatus.PROVEN, result.reason
