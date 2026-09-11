from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zlang.backend.manifest import (
    BackendArtifact,
    DEPENDENCY_MANIFEST_VERSION,
    MANIFEST_VERSION,
)
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_file, compile_source
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity, LOCK_SCHEMA
from zlang.formal_exploration import FormalExplorationConfig, proof_cache_key
from zlang.ir import expressions as expr
from zlang.ir.module import (
    Assignment,
    Module,
    Port,
    PortDirection,
    default_selected_ir_identity,
    dependency_context_identity,
)
from zlang.ir.recursive_formal import (
    build_recursive_formal_design,
    recursive_cache_key,
)
from zlang.ir.types import UIntType
from zlang.opt.lowering import lower, restore
from zlang.opt.render import render
from zlang.synthesis import normalized_candidate_hash
from zlang.workspace import update_project_lock


ROOT = Path(__file__).resolve().parents[2]


def _identity_context(
    dependency_digest: str = "d" * 64,
) -> tuple[DependencyModuleIdentity, DependencyClosure]:
    root = DependencyModuleIdentity(
        "root.Pass",
        "a" * 64,
        "b" * 64,
    )
    dependency = DependencyModuleIdentity(
        "math.Helper",
        dependency_digest,
        "c" * 64,
        "1" * 40,
    )
    return root, DependencyClosure(LOCK_SCHEMA, "e" * 64, (dependency,))


def _module(
    dependency_digest: str = "d" * 64,
    *,
    project: bool = True,
) -> Module:
    type_ = UIntType(8)
    input_ = Port(PortDirection.INPUT, "x", type_)
    output = Port(PortDirection.OUTPUT, "y", type_)
    root, closure = _identity_context(dependency_digest)
    return Module(
        "Pass",
        (input_, output),
        (Assignment(output, expr.InputRef("x", type_)),),
        root_module_identity=root if project else None,
        dependency_closure=closure if project else None,
    )


def test_dependency_identity_round_trips_canonical_and_renders_logically() -> None:
    module = _module()
    canonical = lower(module)

    assert canonical.root_module_identity == module.root_module_identity
    assert canonical.dependency_closure == module.dependency_closure
    assert restore(canonical) == module

    text = render(canonical, include_origins=False)
    assert "root-module logical_path=root.Pass" in text
    assert f"dependency-closure schema={LOCK_SCHEMA}" in text
    assert "dependency-module logical_path=math.Helper" in text
    assert "/home/" not in text


def test_backend_build_identity_includes_closure_but_rtl_hash_does_not() -> None:
    first = emit_artifact(_module("d" * 64))
    changed = emit_artifact(_module("f" * 64))

    assert first.text == changed.text
    assert first.artifact_hash == changed.artifact_hash
    assert first.selected_ir_identity != changed.selected_ir_identity
    assert first.build_identity != changed.build_identity
    assert first.manifest_version == DEPENDENCY_MANIFEST_VERSION

    payload = json.loads(first.to_json())
    assert payload["root_module_identity"] == first.root_module_identity.to_data()
    assert payload["dependency_closure"] == first.dependency_closure.to_data()
    assert payload["build_identity"] == first.build_identity

    restored = BackendArtifact.from_json(payload)
    assert restored.root_module_identity == first.root_module_identity
    assert restored.dependency_closure == first.dependency_closure
    assert restored.build_identity == first.build_identity

    payload["build_identity"] = "0" * 64
    with pytest.raises(ValueError, match="build identity"):
        BackendArtifact.from_json(payload)


def test_legacy_module_and_v2_artifact_defaults_remain_unchanged() -> None:
    module = _module(project=False)
    artifact = emit_artifact(module)

    assert dependency_context_identity(module) is None
    assert default_selected_ir_identity(module) == "selected:Pass"
    assert artifact.selected_ir_identity == "selected:Pass"
    assert artifact.manifest_version == MANIFEST_VERSION
    assert BackendArtifact.from_json(artifact.to_json()).dependency_closure is None


def test_dependency_context_changes_existing_formal_cache_identities() -> None:
    first = _module("d" * 64)
    changed = _module("f" * 64)
    config = FormalExplorationConfig()
    common = dict(
        property_identity="property",
        artifact_hash="0" * 64,
        harness_hash="1" * 64,
        assumptions_identity="none",
        config=config,
    )
    assert proof_cache_key(first, **common) != proof_cache_key(changed, **common)

    candidate = SimpleNamespace(
        implementation_identity="candidate",
        semantic_identity="semantic",
        timing_relation=None,
    )
    explicit_first = replace(
        config,
        dependency_identity=dependency_context_identity(first),
    )
    explicit_changed = replace(
        config,
        dependency_identity=dependency_context_identity(changed),
    )
    explicit_common = {
        key: value for key, value in common.items() if key != "config"
    }
    assert proof_cache_key(
        candidate,
        config=explicit_first,
        **explicit_common,
    ) != proof_cache_key(
        candidate,
        config=explicit_changed,
        **explicit_common,
    )

    first_design = build_recursive_formal_design(first)
    changed_design = build_recursive_formal_design(changed)
    assert first_design.root_module_identity == first.root_module_identity
    assert first_design.dependency_closure == first.dependency_closure
    assert first_design.root_selected_ir_identity != changed_design.root_selected_ir_identity
    cache_args = dict(
        top_artifact_hash="2" * 64,
        formal_artifact_hash="3" * 64,
        harness_hash="4" * 64,
    )
    assert recursive_cache_key(first_design, **cache_args) != recursive_cache_key(
        changed_design,
        **cache_args,
    )


def test_dependency_context_changes_synthesis_candidate_identity() -> None:
    module = compile_source((ROOT / "examples/cost_mac.zhl").read_text()).ir
    first_root, first_closure = _identity_context("d" * 64)
    _, changed_closure = _identity_context("f" * 64)
    first = replace(
        module,
        root_module_identity=first_root,
        dependency_closure=first_closure,
    )
    changed = replace(first, dependency_closure=changed_closure)

    assert normalized_candidate_hash(
        first,
        "y",
        expr.ImplementationKind.MUL_ADD,
    ) != normalized_candidate_hash(
        changed,
        "y",
        expr.ImplementationKind.MUL_ADD,
    )


def test_project_root_source_digest_reaches_artifact_build_identity(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source_root = project / "src"
    source_root.mkdir(parents=True)
    manifest = project / "zlang.toml"
    manifest.write_text(
        "schema = 1\n\n"
        "[project]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n'
        'source-root = "src"\n'
    )
    source = source_root / "top.zhl"
    source.write_text("module Top { in x:u8 out y:u8 y=x }\n")
    update_project_lock(manifest)

    first_module = compile_file(source).ir
    first = emit_artifact(first_module)
    source.write_text("// Same design, different exact root source.\n" + source.read_text())
    changed_module = compile_file(source).ir
    changed = emit_artifact(changed_module)

    assert first_module.root_module_identity is not None
    assert first_module.root_module_identity.logical_path == "demo.top"
    assert first_module.dependency_closure is not None
    assert first.text == changed.text
    assert first.artifact_hash == changed.artifact_hash
    assert first.root_module_identity.digest != changed.root_module_identity.digest
    assert first.selected_ir_identity != changed.selected_ir_identity
    assert first.build_identity != changed.build_identity
