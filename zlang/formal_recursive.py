"""Capacity-bounded recursive formal harness publication."""

from __future__ import annotations

from zlang.ir.formal import FormalError, FormalStatus, ProofMode
from zlang.ir.recursive_formal import RecursiveFormalDesign, RecursiveFormalResult


def emit_recursive_harness(
    design: RecursiveFormalDesign,
    *,
    mode: ProofMode = ProofMode.BMC,
    depth: int = 20,
) -> str:
    """Emit the deterministic whole-top observation harness skeleton."""

    if depth < 1:
        raise FormalError("formal depth must be positive")
    module_name = (
        f"{design.root_instance_identity}__recursive_safety_verification_formal"
    )
    lines = [
        "`default_nettype none",
        f"module {module_name}(input wire clock, input wire reset,",
    ]
    observations = sorted(
        design.bindings, key=lambda item: item.semantic_binding_id
    )
    ports = [
        f"  input wire [{item.width - 1}:0] zlang_formal_obs_{index}"
        for index, item in enumerate(observations)
    ]
    lines.append(",\n".join(ports) + ");")
    lines.append(f"  // schema={design.schema_version} mode={mode.value} depth={depth}")
    for index, item in enumerate(observations):
        lines.append(
            f"  // observation {item.semantic_binding_id} = zlang_formal_obs_{index}"
        )
    for item in sorted(
        design.properties, key=lambda value: value.concrete_property_id
    ):
        statement = (
            "assume" if item.property.kind.value == "assumption" else "assert"
        )
        lines.append(
            f"  // {statement} {item.concrete_property_id} owned_by={item.ownership}"
        )
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def run_recursive_formal(
    design: RecursiveFormalDesign,
    *,
    mode: ProofMode = ProofMode.BMC,
    depth: int = 20,
    reason: str | None = None,
    artifact: object | None = None,
) -> tuple[RecursiveFormalResult, ...]:
    """Fail closed until all recursive observations are backend-connected."""

    if artifact is not None:
        unavailable = tuple(
            item.semantic_binding_id
            for item in design.bindings
            if not any(
                observation.semantic_binding_id == item.semantic_binding_id
                and observation.observation_token is not None
                for observation in getattr(artifact, "formal_observations", ())
            )
        )
        if unavailable and reason is None:
            reason = "formal observations unavailable: " + ", ".join(unavailable[:4])
    why = reason or "formal observation artifact is not connected to generated RTL"
    return tuple(
        RecursiveFormalResult(
            item.concrete_property_id,
            item.source_property_id,
            FormalStatus.SKIPPED,
            mode,
            "sby",
            "z3",
            depth,
            item.defining_module,
            item.specialization_identity,
            item.instance_identity,
            item.physical_instance_path,
            item.property.source_origin,
            reason=why,
        )
        for item in design.properties
    )
