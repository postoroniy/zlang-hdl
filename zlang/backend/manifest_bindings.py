"""Physical ABI binding construction for backend artifacts."""

from __future__ import annotations

from dataclasses import replace
import re

from zlang.ir.equivalence import BindingSide, EquivalenceBinding, SignalRole
from zlang.ir.module import Module, PortDirection
from zlang.ir.physical_types import physical_width
from zlang.ir.types import HardwareType, TaggedUnionType


def physical_width_of(type_: HardwareType) -> int:
    return physical_width(type_)


def canonical_type(type_: HardwareType) -> str:
    """Render a manifest type without erasing nominal enum identity."""

    if isinstance(type_, TaggedUnionType):
        variants = "|".join(
            variant.name
            + (
                "(" + ",".join(
                    f"{field.name}:{field.type}" for field in variant.fields
                ) + ")"
                if variant.fields else ""
            )
            for variant in type_.variants
        )
        return f"union<{type_.name}:{variants}@{type_.declaration_identity}>"
    return str(type_)


def physical_top_bindings(
    module: Module,
    bindings: list[EquivalenceBinding],
    *,
    names: dict[str, str],
    artifact_version: int,
    side: BindingSide,
    selected_ir_identity: str,
    backend: str,
    digest: str,
    text: str,
    validated_physical_paths: frozenset[str],
) -> list[EquivalenceBinding]:
    """Bind and validate every public physical ABI leaf exactly once."""

    physical_leaves = tuple(module.top_physical_abi.leaves)
    leaves_by_semantic = {leaf.leaf_semantic_id: leaf for leaf in physical_leaves}
    leaves_by_root: dict[str, list[object]] = {}
    for leaf in physical_leaves:
        leaves_by_root.setdefault(leaf.packed_root_semantic_id, []).append(leaf)
    split_roots = {
        root
        for root, root_leaves in leaves_by_root.items()
        if not (
            len(root_leaves) == 1
            and root_leaves[0].leaf_semantic_id == root
            and root_leaves[0].external_name
            == root_leaves[0].packed_root_external_name
        )
    }
    rebound: list[EquivalenceBinding] = []
    rebound_ids: set[str] = set()
    for binding in bindings:
        leaf = leaves_by_semantic.get(binding.semantic_signal_id)
        if leaf is not None:
            binding = replace(
                binding,
                map_version=artifact_version,
                rtl_module=module.name,
                rtl_path=names.get(leaf.leaf_semantic_id, leaf.external_name),
                width=leaf.width,
                signedness=leaf.signedness,
                role=(
                    SignalRole.INPUT
                    if leaf.direction is PortDirection.INPUT
                    else SignalRole.OUTPUT
                ) if leaf.signal_kind not in {"clock", "reset"} else (
                    SignalRole.CLOCK
                    if leaf.signal_kind == "clock" else SignalRole.RESET
                ),
                clock_domain=leaf.clock_domain,
                reset_domain=leaf.reset_domain,
                source_origin=leaf.source_origin or binding.source_origin,
                aggregate_endpoint_id=(
                    leaf.aggregate_id if leaf.category == "aggregate" else None
                ),
                protocol_specialization_id=leaf.protocol_specialization_id,
                protocol_role=leaf.role,
                member_path=leaf.member_path,
                ownership=leaf.ownership,
                signal_kind=leaf.signal_kind,
                physical_available=True,
                canonical_type=canonical_type(leaf.canonical_type),
            )
        elif binding.semantic_signal_id in split_roots:
            binding = replace(binding, rtl_path="", physical_available=False)
        rebound.append(binding)
        rebound_ids.add(binding.semantic_signal_id)
    for leaf in physical_leaves:
        if leaf.leaf_semantic_id in rebound_ids:
            continue
        rebound.append(EquivalenceBinding(
            artifact_version,
            side,
            leaf.leaf_semantic_id,
            selected_ir_identity,
            module.name,
            names.get(leaf.leaf_semantic_id, leaf.external_name),
            leaf.width,
            leaf.signedness,
            (
                SignalRole.CLOCK if leaf.signal_kind == "clock" else
                SignalRole.RESET if leaf.signal_kind == "reset" else
                SignalRole.INPUT if leaf.direction is PortDirection.INPUT else
                SignalRole.OUTPUT
            ),
            leaf.clock_domain,
            leaf.reset_domain,
            backend,
            digest,
            leaf.source_origin,
            leaf.aggregate_id if leaf.category == "aggregate" else None,
            leaf.protocol_specialization_id,
            leaf.role,
            leaf.member_path,
            leaf.ownership,
            leaf.signal_kind,
            True,
            canonical_type(leaf.canonical_type),
        ))

    validated: list[EquivalenceBinding] = []
    for binding in rebound:
        token = binding.rtl_path.rsplit(".", 1)[-1] if binding.rtl_path else ""
        available = bool(token) and (
            binding.rtl_path in validated_physical_paths
            or re.search(
                rf"(?<![A-Za-z0-9_$]){re.escape(token)}(?![A-Za-z0-9_$])",
                text,
            )
        )
        validated.append(replace(
            binding,
            physical_available=binding.physical_available and bool(available),
            rtl_path=binding.rtl_path if available else "",
        ))
    return validated


__all__ = ["canonical_type", "physical_top_bindings", "physical_width_of"]
