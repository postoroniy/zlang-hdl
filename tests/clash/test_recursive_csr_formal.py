"""Semantic CSR field identity through recursive Clash/direct-SV M35."""

from dataclasses import replace
from pathlib import Path
import shutil
import tempfile

import pytest

from zlang import compile_source
from zlang.backend.clash import (
    ClashEmissionError,
    emit_artifact,
    emit_formal_artifact,
    run_recursive_register_formal,
    validate_register_formal_artifact,
)
from zlang.backend.systemverilog import emit_formal_artifact as emit_sv_formal_artifact
from zlang.formal import build_recursive_formal_design
from zlang.ir.formal import FormalStatus
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
TOOLS = bool(find_clash_executable() and shutil.which("verilator"))
FORMAL_TOOLS = bool(TOOLS and all(shutil.which(item) for item in ("yosys", "sby", "z3")))

WRAPPER = """import std.bus.reg
module CsrFormalTop {
  clock clk reset rst
  in addr:u32 in write:bit in wdata:u32 in read:bit
  out value:bit
  inst bank:RegBusCSRBank { addr=addr write=write wdata=wdata read=read }
  value=bank.csr_field_0_0_0_state
}
"""

SIBLINGS = """import std.bus.reg
module SiblingCsrTop {
  clock clk reset rst
  in addr0:u32 in write0:bit in addr1:u32 in write1:bit
  in wdata:u32 in read:bit out value:bit
  inst bank0:RegBusCSRBank { addr=addr0 write=write0 wdata=wdata read=read }
  inst bank1:RegBusCSRBank { addr=addr1 write=write1 wdata=wdata read=read }
  value=bank0.csr_field_0_0_0_state
}
"""


def compiled():
    module = compile_source(WRAPPER, top="CsrFormalTop").ir
    design = build_recursive_formal_design(module)
    return module, design, emit_formal_artifact(module, design)


def csr_property_ids(design, suffix=None):
    return frozenset(
        item.concrete_property_id
        for item in design.properties
        if (item.property.generated_from or "").startswith("csr-field:")
        and (suffix is None or (item.property.generated_from or "").endswith(suffix))
    )


def test_recursive_identity_has_state_event_and_implementation_provenance():
    module, design, artifact = compiled()
    csr = [item for item in design.bindings
           if item.ref.local_semantic_id.startswith("csr-field:")]
    assert len(csr) == 9
    assert {item.ref.object_kind for item in csr} == {
        "csr_field_state", "csr_access_event"
    }
    assert all(item.ref.implementation_state_id for item in csr)
    assert all(item.ref.source_origin is not None for item in csr)
    assert all(item.physical_instance_path == ("CsrFormalTop", "bank")
               for item in csr)
    assert emit_artifact(module).text == emit_artifact(module).text
    assert emit_artifact(module).artifact_hash != artifact.formal_artifact_hash


def test_recursive_csr_lookup_rejects_specialization_mismatch():
    module = compile_source(WRAPPER, top="CsrFormalTop").ir
    design = build_recursive_formal_design(module)
    child_index = next(
        index
        for index, node in enumerate(design.instances)
        if node.physical_instance_path == ("CsrFormalTop", "bank")
    )
    nodes = list(design.instances)
    nodes[child_index] = replace(
        nodes[child_index], specialization_identity="wrong-specialization"
    )
    malformed = replace(design, instances=tuple(nodes))
    with pytest.raises(ClashEmissionError, match="specialization"):
        emit_formal_artifact(module, malformed)


@pytest.mark.skipif(not TOOLS, reason="Clash and Verilator are required")
def test_recursive_clash_and_direct_sv_publish_the_same_csr_identities():
    module, design, clash = compiled()
    with tempfile.TemporaryDirectory() as directory:
        files = generate_verilog(
            clash.text, "CsrFormalTop_formal", Path(directory),
            find_clash_executable(),
        )
        lint_with_verilator(files, "CsrFormalTop_formal")
        clash = validate_register_formal_artifact(clash, files)
    direct = emit_sv_formal_artifact(module, design)
    clash_ids = {
        item.semantic_binding_id for item in clash.formal_observations
        if item.physical_available and ":csr-field:" in item.semantic_binding_id
    }
    direct_ids = {
        item.semantic_binding_id for item in direct.formal_observations
        if item.physical_available and ":csr-field:" in item.semantic_binding_id
    }
    assert len(clash_ids) == 9
    assert direct_ids == clash_ids


@pytest.mark.skipif(not TOOLS, reason="Clash and Verilator are required")
def test_axi_and_apb_formal_tops_publish_all_recursive_csr_leaves():
    for filename in ("axi_csr_top.zhl", "apb_csr_top.zhl"):
        module = compile_source((ROOT / "examples" / filename).read_text()).ir
        design = build_recursive_formal_design(module)
        artifact = emit_formal_artifact(module, design)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, f"{module.name}_formal", Path(directory),
                find_clash_executable(),
            )
            lint_with_verilator(files, f"{module.name}_formal")
            validated = validate_register_formal_artifact(artifact, files)
        assert sum(
            item.physical_available and ":csr-field:" in item.semantic_binding_id
            for item in validated.recursive_bindings
        ) == 9


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real Clash/SBY/Z3 unavailable")
def test_real_apb_regbus_csr_rw_property_executes_through_full_hierarchy():
    module = compile_source((ROOT / "examples/apb_csr_top.zhl").read_text()).ir
    design = build_recursive_formal_design(module)
    artifact = emit_formal_artifact(module, design)
    rw = next(
        item for item in design.properties
        if (item.property.generated_from or "").endswith(":rw")
    )
    with tempfile.TemporaryDirectory() as directory:
        files = generate_verilog(
            artifact.text, f"{module.name}_formal", Path(directory),
            find_clash_executable(),
        )
        result = run_recursive_register_formal(
            module, design, artifact, files, depth=4,
            property_ids=frozenset((rw.concrete_property_id,)),
        )[0]
    assert result.status is FormalStatus.BOUNDED_PASS
    assert result.physical_instance_path[-2:] == ("csr", "bank")


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real Clash/SBY/Z3 unavailable")
def test_real_recursive_rw_w1c_and_pulse_properties_pass():
    module, design, artifact = compiled()
    with tempfile.TemporaryDirectory() as directory:
        files = generate_verilog(
            artifact.text, "CsrFormalTop_formal", Path(directory),
            find_clash_executable(),
        )
        results = run_recursive_register_formal(
            module, design, artifact, files, depth=6,
            property_ids=csr_property_ids(design),
        )
    assert len(results) == 6
    assert all(item.status is FormalStatus.BOUNDED_PASS for item in results)
    assert {item.source_origin.construct for item in results} == {
        "CSR field registers.RW.value",
        "CSR field registers.W1C.value",
        "CSR field registers.PULSE.value",
    }


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real Clash/SBY/Z3 unavailable")
@pytest.mark.parametrize(
    ("suffix", "old", "new"),
    (
        (
            ":w1c",
            "csr_registers_w1c_write_hit ? (csr_registers_w1c_value & (~ csr_registers_pulse_value_write_value)) : csr_registers_w1c_value",
            "csr_registers_w1c_write_hit ? csr_registers_pulse_value_write_value : csr_registers_w1c_value",
        ),
        (
            ":pulse",
            "csr_registers_pulse_write_hit ? csr_registers_pulse_value_write_value : 1'b0",
            "csr_registers_pulse_write_hit ? csr_registers_pulse_value_write_value : csr_registers_pulse_value",
        ),
    ),
)
def test_real_csr_mutations_fail_with_semantic_attribution(suffix, old, new):
    module, design, artifact = compiled()
    with tempfile.TemporaryDirectory() as directory:
        files = generate_verilog(
            artifact.text, "CsrFormalTop_formal", Path(directory),
            find_clash_executable(),
        )
        top = next(path for path in files if path.name == "CsrFormalTop_formal.v")
        original = top.read_text()
        mutated = original.replace(old, new)
        assert mutated != original
        top.write_text(mutated)
        results = run_recursive_register_formal(
            module, design, artifact, files, depth=6,
            property_ids=csr_property_ids(design, suffix),
        )
    assert len(results) == 1
    result = results[0]
    assert result.status is FormalStatus.FAILED
    assert result.physical_instance_path == ("CsrFormalTop", "bank")
    assert result.source_origin is not None
    assert result.counterexample is not None
    assert result.counterexample.raw_trace
    assert result.counterexample.cycle is not None
    assert any(key.startswith("implementation:csr-state:")
               for key, _ in result.object_values)


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real Clash/SBY/Z3 unavailable")
def test_identical_specialization_sibling_mutation_is_attributed_only_to_bank0():
    module = compile_source(SIBLINGS, top="SiblingCsrTop").ir
    design = build_recursive_formal_design(module)
    artifact = emit_formal_artifact(module, design)
    with tempfile.TemporaryDirectory() as directory:
        files = generate_verilog(
            artifact.text, "SiblingCsrTop_formal", Path(directory),
            find_clash_executable(),
        )
        top = next(path for path in files if path.name == "SiblingCsrTop_formal.v")
        original = top.read_text()
        old = (
            "csr_registers_w1c_write_hit_0 ? "
            "(csr_registers_w1c_value_0 & (~ csr_registers_pulse_value_write_value_0)) "
            ": csr_registers_w1c_value_0"
        )
        new = (
            "csr_registers_w1c_write_hit_0 ? "
            "csr_registers_pulse_value_write_value_0 : csr_registers_w1c_value_0"
        )
        mutated = original.replace(old, new)
        assert mutated != original
        top.write_text(mutated)
        results = run_recursive_register_formal(
            module, design, artifact, files, depth=6,
            property_ids=csr_property_ids(design, ":w1c"),
        )
    assert len(results) == 2
    by_instance = {item.physical_instance_path[-1]: item for item in results}
    assert by_instance["bank0"].status is FormalStatus.FAILED
    assert by_instance["bank1"].status is FormalStatus.BOUNDED_PASS
    assert by_instance["bank0"].specialization_identity == \
        by_instance["bank1"].specialization_identity
    assert by_instance["bank0"].instance_identity != \
        by_instance["bank1"].instance_identity
