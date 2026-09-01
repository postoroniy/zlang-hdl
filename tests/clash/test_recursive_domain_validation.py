"""Fail-closed recursive formal clock/reset-domain validation."""

from dataclasses import replace
from pathlib import Path
import tempfile

import pytest

from zlang.backend.clash import emit_formal_artifact, run_recursive_register_formal
from zlang.backend.clash.formal_registers import (
    _recursive_domain_mismatch_reason,
    _recursive_physical_domain_artifact_reason,
)
from zlang.formal import build_recursive_formal_design
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.toolchain import find_clash_executable, generate_verilog


SOURCE = (
    "module Child { clock c reset r out y:u8 reg q:u8=0 q <- q y=q } "
    "module Top { clock c reset r out y:u8 inst child:Child y=child.y }"
)


def _fixture():
    module = analyze(parse(SOURCE))
    design = build_recursive_formal_design(module)
    artifact = emit_formal_artifact(module, design)
    child = next(
        item for item in design.instances if item.module_name == "Child"
    )
    concrete = next(
        item for item in design.properties
        if item.instance_identity == child.instance_identity
    )
    bindings = {
        item.semantic_binding_id: item for item in artifact.recursive_bindings
    }
    instances = {
        item.instance_identity: item for item in artifact.instances
    }
    return module, design, child, concrete, bindings, instances


def test_same_domain_recursive_property_is_compatible() -> None:
    module, design, _, concrete, bindings, instances = _fixture()
    assert _recursive_domain_mismatch_reason(
        module, design, concrete, bindings, instances
    ) is None


def test_recursive_instance_domain_is_not_silently_remapped() -> None:
    module, design, child, concrete, bindings, instances = _fixture()
    incompatible = replace(
        design,
        instances=tuple(
            replace(item, clock_domain="other_clock")
            if item.instance_identity == child.instance_identity else item
            for item in design.instances
        ),
    )
    reason = _recursive_domain_mismatch_reason(
        module, incompatible, concrete, bindings, instances
    )
    assert reason is not None
    assert "instance domain does not match connected root" in reason
    assert "other_clock" in reason


def test_recursive_property_clock_is_not_silently_remapped() -> None:
    module, design, _, concrete, bindings, instances = _fixture()
    incompatible = replace(
        concrete,
        property=replace(concrete.property, clock="other_clock"),
    )
    reason = _recursive_domain_mismatch_reason(
        module, design, incompatible, bindings, instances
    )
    assert reason is not None
    assert "property clock does not match connected root" in reason


def test_recursive_observation_reset_domain_is_not_silently_remapped() -> None:
    module, design, _, concrete, bindings, instances = _fixture()
    semantic_id = concrete.property.relevant_signals[0]
    incompatible = dict(bindings)
    incompatible[semantic_id] = replace(
        incompatible[semantic_id], reset_domain="other_reset"
    )
    reason = _recursive_domain_mismatch_reason(
        module, design, concrete, incompatible, instances
    )
    assert reason is not None
    assert "observation domain does not match connected root" in reason
    assert semantic_id in reason
    assert "other_reset" in reason


def test_connected_artifact_instance_domain_is_not_silently_remapped() -> None:
    module, design, child, concrete, bindings, instances = _fixture()
    incompatible = dict(instances)
    incompatible[child.instance_identity] = replace(
        incompatible[child.instance_identity], clock_domain="other_clock"
    )
    reason = _recursive_domain_mismatch_reason(
        module, design, concrete, bindings, incompatible
    )
    assert reason is not None
    assert "artifact instance domain does not match root" in reason


def test_recursive_property_reset_is_not_silently_remapped() -> None:
    module, design, _, concrete, bindings, instances = _fixture()
    incompatible = replace(
        concrete,
        property=replace(concrete.property, reset_condition="other_reset"),
    )
    reason = _recursive_domain_mismatch_reason(
        module, design, incompatible, bindings, instances
    )
    assert reason is not None
    assert "property reset does not match connected root" in reason


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("clock_edge", "falling", "invalid"),
        ("reset_polarity", "active_low", "invalid"),
        ("reset_release_mode", "native", "invalid"),
        ("reset_release_cycles", 1, "invalid"),
        ("rtl_clock_path", "other_clock", "paths do not match"),
        ("rtl_reset_path", "other_reset", "paths do not match"),
    ),
)
def test_recursive_execution_rejects_corrupted_physical_domain_manifest(
    field: str,
    value: object,
    message: str,
) -> None:
    async_source = SOURCE.replace("reset r", "async reset r @c")
    module = analyze(parse(async_source))
    design = build_recursive_formal_design(module)
    artifact = emit_formal_artifact(module, design)
    assert len(artifact.physical_domains) == 1
    corrupted = replace(
        artifact,
        physical_domains=(
            replace(artifact.physical_domains[0], **{field: value}),
        ),
    )
    reason = _recursive_physical_domain_artifact_reason(module, corrupted)
    assert reason is not None
    assert message in reason


@pytest.mark.skipif(find_clash_executable() is None, reason="Clash is unavailable")
def test_recursive_execution_reports_domain_mismatch_as_skipped() -> None:
    module, design, child, _, _, _ = _fixture()
    incompatible = replace(
        design,
        instances=tuple(
            replace(item, clock_domain="other_clock")
            if item.instance_identity == child.instance_identity else item
            for item in design.instances
        ),
    )
    artifact = emit_formal_artifact(module, design)
    with tempfile.TemporaryDirectory() as directory:
        files = generate_verilog(
            artifact.text, "Top_formal", Path(directory), find_clash_executable()
        )
        results = run_recursive_register_formal(
            module, incompatible, artifact, files, depth=2
        )
    child_results = [
        item for item in results if item.instance_identity == child.instance_identity
    ]
    assert child_results
    assert all(item.status.value == "skipped" for item in child_results)
    assert all(
        "instance domain does not match connected root" in (item.reason or "")
        for item in child_results
    )
