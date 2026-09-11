from pathlib import Path

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact, emit_experimental, emit_formal_artifact
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design


ROOT = Path(__file__).resolve().parents[2]
UNBUFFERED = (ROOT / "examples/hierarchical_request_response_m40.zhl").read_text()


def _module(maximum: int):
    source = (ROOT / "examples/simple_dma_m40.zhl").read_text().replace(
        "max_outstanding 2", f"max_outstanding {maximum}"
    )
    return compile_source(source, top="SimpleDMA").ir


def test_direct_hierarchy_emits_accepted_outstanding_ledger_for_supported_limits():
    for maximum in (1, 2, 4):
        generated = emit_experimental(_module(maximum))
        assert "rr_engine_mem_engine_outstanding" in generated
        assert "request_transfer" in generated
        assert "response_transfer" in generated
        assert f"'d{maximum}" in generated


def test_recursive_manifest_marks_unmaterialized_observations_unavailable():
    module = _module(2)
    artifact = emit_artifact(module, recursive_design=build_recursive_formal_design(module))
    assert all(item.observation_token is None for item in artifact.formal_observations)
    rr = next(item for item in artifact.recursive_bindings if item.local_semantic_id.endswith(":outstanding"))
    assert rr.signal_token == "rr_engine_mem_engine_outstanding"
    assert rr.formal_observation_token is None


def test_formal_artifact_materializes_nested_observations_as_explicit_component_ports():
    module = _module(2)
    artifact = emit_formal_artifact(module, build_recursive_formal_design(module))
    assert "module SimpleDMA__formal" in artifact.text
    assert "zlang_formal_local_" in artifact.text
    assert "dut.engine.count" not in artifact.text
    assert any(item.observation_token is not None for item in artifact.formal_observations)
    assert artifact.formal_artifact_hash == artifact.artifact_hash


def test_unbuffered_directional_occupancy_is_a_typed_zero_projection():
    module = compile_source(
        UNBUFFERED.replace("max_outstanding 1", "max_outstanding 2"),
        top="HierarchicalRequestResponse",
    ).ir
    recursive = build_recursive_formal_design(module)
    production_before = emit_artifact(module, recursive_design=recursive)
    formal = emit_formal_artifact(module, recursive)
    production_after = emit_artifact(module, recursive_design=recursive)

    # Formal instrumentation must not perturb the production implementation.
    assert production_after == production_before
    bindings = {
        item.local_semantic_id.rsplit(":", 1)[-1]: item
        for item in formal.recursive_bindings
        if item.local_semantic_id.startswith("rr:")
    }
    assert set(bindings) == {
        "outstanding",
        "request_accept",
        "response_consume",
        "request_occupancy",
        "response_occupancy",
    }
    assert all(item.physical_available for item in bindings.values())
    for name in ("request_occupancy", "response_occupancy"):
        binding = bindings[name]
        assert binding.width == 2
        assert binding.formal_observation_token is not None
        assert (
            f"assign {binding.formal_observation_token} = 2'd0;"
            in formal.text
        )

    restored = BackendArtifact.from_json(formal.to_json())
    restored_bindings = {
        item.local_semantic_id: item
        for item in restored.recursive_bindings
        if item.local_semantic_id.startswith("rr:")
    }
    assert restored_bindings == {
        item.local_semantic_id: item
        for item in formal.recursive_bindings
        if item.local_semantic_id.startswith("rr:")
    }


def test_buffered_directional_occupancy_uses_an_explicit_formal_fifo_count_abi():
    module = _module(2)
    recursive = build_recursive_formal_design(module)
    production_before = emit_artifact(module, recursive_design=recursive)
    formal = emit_formal_artifact(module, recursive)
    production_after = emit_artifact(module, recursive_design=recursive)
    bindings = {
        item.local_semantic_id.rsplit(":", 1)[-1]: item
        for item in formal.recursive_bindings
        if item.local_semantic_id.startswith("rr:")
    }
    assert production_after == production_before
    assert "ZLangRvFifoFormal" not in production_before.text
    assert "formal_count" not in production_before.text
    assert all(item.physical_available for item in bindings.values())
    assert bindings["request_occupancy"].width == 3
    assert bindings["response_occupancy"].width == 2
    assert all(item.source_origin is not None for item in bindings.values())
    assert formal.text.count("output logic [2:0] formal_count") == 1
    assert formal.text.count("output logic [1:0] formal_count") == 1
    assert formal.text.count(".formal_count(") == 2
    for name in ("request_occupancy", "response_occupancy"):
        token = bindings[name].formal_observation_token
        assert token is not None
        assert f"assign {token} = zlang_formal_rr_buffer_count_" in formal.text
    assert ".count" not in formal.text


def test_request_and_response_count_projections_are_directionally_independent():
    variants = (
        (
            "request_buffer 4",
            "request_occupancy",
            "response_occupancy",
            3,
            2,
        ),
        (
            "response_buffer 2",
            "response_occupancy",
            "request_occupancy",
            2,
            2,
        ),
    )
    for option, buffered, unbuffered, buffered_width, zero_width in variants:
        source = UNBUFFERED.replace(
            "max_outstanding 1", "max_outstanding 2"
        ).replace(
            "connect requester.bus -> responder.bus",
            f"connect requester.bus -> responder.bus {{ {option} }}",
        )
        module = compile_source(
            source,
            top="HierarchicalRequestResponse",
        ).ir
        formal = emit_formal_artifact(
            module, build_recursive_formal_design(module)
        )
        bindings = {
            item.local_semantic_id.rsplit(":", 1)[-1]: item
            for item in formal.recursive_bindings
            if item.local_semantic_id.startswith("rr:")
        }
        assert formal.text.count(".formal_count(") == 1
        assert bindings[buffered].width == buffered_width
        assert bindings[unbuffered].width == zero_width
        assert bindings[buffered].physical_available
        assert bindings[unbuffered].physical_available
        zero_token = bindings[unbuffered].formal_observation_token
        assert zero_token is not None
        assert (
            f"assign {zero_token} = {zero_width}'d0;" in formal.text
        )
