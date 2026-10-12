"""Deterministic target architecture selection and mapping."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from zlang.ir.module import Module
from zlang.ir import target as target_ir

from zlang import target_catalog as catalog
from zlang.target_mapping_fir import map_manual_architecture
from zlang import target_mapping_identity as mapping_identity


def generic_implementation_graph(module: Module, target: target_ir.TargetInstance | None = None) -> target_ir.ImplementationGraph:
    semantic = sha256(
        (
            "zlang-generic-module-physical-v2:"
            + mapping_identity._generic_physical_digest(module)
        ).encode()
    ).hexdigest()
    latency_knowledge, latency = mapping_identity._module_implementation_latency(module)
    return target_ir.ImplementationGraph(
        semantic_region_identity=semantic,
        architecture_template_identity="std.arch.generic",
        target_identity=target.identity if target else None,
        target_hash=target.source_hash if target else None,
        resource_definition_hashes=(), resources=(), dedicated_edges=(),
        latency=latency, initiation_interval=1,
        realization_backend="backend_independent",
        latency_knowledge=latency_knowledge,
        legality_evidence=("technology-independent generic implementation",),
        target_dependency_hashes=target.dependency_hashes if target else (),
        selection_policy=catalog.ArchitectureSelectionMode.GENERIC.value,
        target_part=target.part if target else None,
    )


def select_implementation_graph(
    module: Module,
    *,
    target: str | None = None,
    architecture: str | None = None,
    mode: catalog.ArchitectureSelectionMode | str = catalog.ArchitectureSelectionMode.GENERIC,
) -> target_ir.ImplementationGraph:
    policy = catalog.ArchitectureSelectionMode(mode)
    selected_target = None
    family = None
    resources: tuple[target_ir.ResourceDefinition, ...] = ()
    if target is not None:
        selected_target, family, resources = catalog.load_target(target)
    generic = generic_implementation_graph(module, selected_target)
    if policy is catalog.ArchitectureSelectionMode.GENERIC:
        return generic
    if architecture is None:
        if policy is catalog.ArchitectureSelectionMode.REQUIRED:
            raise catalog.TargetArchitectureError("required architecture is unavailable: no architecture was selected")
        return replace(
            generic, selection_policy=policy.value,
            legality_evidence=(
                *generic.legality_evidence,
                "no manual architecture was selected",
            ),
        ) if policy is catalog.ArchitectureSelectionMode.PREFERRED else generic
    if selected_target is None:
        error = catalog.TargetArchitectureError(
            f"architecture '{architecture}' requires an explicit target"
        )
        if policy is catalog.ArchitectureSelectionMode.PREFERRED:
            return replace(
                generic, selection_policy=policy.value,
                legality_evidence=(*generic.legality_evidence, f"preferred architecture rejected: {error}"),
            )
        raise error
    try:
        template = catalog.load_architecture(architecture)
        return map_manual_architecture(
            module, selected_target, family, resources, template, policy
        )
    except catalog.TargetArchitectureError as error:
        if policy is catalog.ArchitectureSelectionMode.PREFERRED:
            return replace(
                generic, selection_policy=policy.value,
                legality_evidence=(*generic.legality_evidence, f"preferred architecture rejected: {error}"),
            )
        raise
