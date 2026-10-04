# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Construction of the deterministic executable native-simulation plan."""

from __future__ import annotations

from dataclasses import replace
import platform
from typing import Any

from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.packing import PACKING_LAYOUT_SCHEMA
from zlang.ir.verification import VerificationGoalKind
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.ir import OptimizationStage
from zlang.opt.module_lowering import lower
from zlang import simulation_lowering as simulation_lowering
from zlang.simulation_expression_plan import (
    PrimitiveExpressionPlanBuilder,
)
from zlang.simulation_plan_policy import (
    DEFAULT_SIMULATION_PLAN_POLICY,
    JitUnsupportedFeatureError,
    SimulationPlanPolicy,
    SimulationPlanError,
)
from zlang.simulation_plan_model import SimulationPlan, identity_bytes
from zlang.simulation_plan_codec import validate_plan_payload as _validate_plan_payload
from zlang.simulation_runtime_plan import RuntimeStatePlanBuilder
from zlang.simulation_storage_plan import StoragePlanBuilder
from zlang.simulation_transition_plan import TransitionPlanBuilder
from zlang.simulation_verification_plan import VerificationOverlayBuilder


_NATIVE_PLAN_OPS = {"fifo_ref", "memory_port_read", "rom_lookup"}


def _unsupported_module_features(module: object) -> tuple[str, ...]:
    features: list[str] = []
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        features.append("protocol ports")
    checks = (
        ("request/response interfaces", module.request_responses),
        ("connections", module.connections),
        ("CSR blocks", module.csr_blocks),
        ("packet arbiters", module.arbiters),
        ("instances", module.elaborated_instances or module.instances),
        ("hierarchical children", module.children),
        ("hierarchical connections", module.hierarchical_connections),
        ("aggregate protocol connections", module.aggregate_protocol_connections),
        ("request/response connections", module.request_response_connections),
        ("elastic pipelines", module.elastic_pipeline_regions),
    )
    features.extend(name for name, value in checks if value)
    if module.external_contract is not None:
        features.append("external module contract")
    return tuple(features)


def _lower_direct_wire_connections(module: object) -> object:
    """Erase semantically validated direct wire edges into ordinary assignments.

    Buffered, adapted, crossed, and protocol connections have dedicated
    lowering paths.  A plain wire connection is exactly an input reference
    driving the destination and needs no runtime instruction of its own.
    """

    from zlang.ir.expressions import InputRef
    from zlang.ir.module import Assignment

    assignments = list(module.assignments)
    remaining = []
    for connection in module.connections:
        direct = (
            connection.source.protocol is InterfaceProtocol.WIRE
            and connection.destination.protocol is InterfaceProtocol.WIRE
            and connection.buffer_depth == 0
            and connection.adapter is None
            and connection.crossing is None
        )
        if not direct:
            remaining.append(connection)
            continue
        assignments.append(
            Assignment(
                connection.destination,
                InputRef(connection.source.name, connection.source.type),
            )
        )
    if len(remaining) == len(module.connections):
        return module
    return replace(
        module,
        assignments=tuple(assignments),
        connections=tuple(remaining),
    )


def _erase_non_runtime_metadata(module: object) -> object:
    """Validate and erase compiler/formal metadata from executable planning.

    Equivalence rules are exact compile-time rewrite declarations. Historical
    ``Contract`` records are likewise not an independent execution surface:
    semantic analysis mirrors them into the verification overlay consumed by
    generic runtime probes.  Validate that mirror before discarding either
    record family so simulation cannot silently lose a source check.
    """

    clauses = [
        (scope, "requirement", clause)
        for scope in module.verification_scopes
        for clause in scope.requirements
    ]
    clauses.extend(
        (scope, "goal", clause)
        for scope in module.verification_scopes
        for clause in scope.goals
    )
    for contract in module.contracts:
        expected_kind = (
            "requirement" if contract.kind.value == "assume" else "goal"
        )
        matches = [
            (scope, clause)
            for scope, kind, clause in clauses
            if kind == expected_kind
            and scope.clock == contract.clock
            and scope.reset == contract.reset
            and clause.name == contract.name
            and clause.expression == contract.expression
            and (
                expected_kind == "requirement"
                or clause.kind is VerificationGoalKind.ASSERT
            )
        ]
        if len(matches) != 1:
            raise JitUnsupportedFeatureError(
                f"verification contract '{contract.name}' has no exact "
                "runtime verification-overlay mirror"
            )
    return replace(module, contracts=(), equivalences=())


def build_simulation_plan(
    module: object,
    *,
    policy: SimulationPlanPolicy = DEFAULT_SIMULATION_PLAN_POLICY,
) -> SimulationPlan:
    """Build a strict executable plan from a post-planning semantic module.

    Static wire hierarchy is erased compiler-side before the primitive plan
    crosses the runtime boundary.  The native executor therefore consumes the
    same language-neutral schema for leaf and hierarchical designs.
    """

    max_nodes = policy.max_nodes
    max_bytes = policy.max_bytes

    from zlang.simulation_cdc import (
        CdcSimulationLoweringError,
        lower_cdc_module,
    )

    try:
        module = lower_cdc_module(module)
    except CdcSimulationLoweringError as error:
        raise JitUnsupportedFeatureError(str(error)) from error

    if module.request_responses and not (
        module.elaborated_instances or module.instances or module.children
    ):
        from zlang.simulation_protocol_shared import ProtocolSimulationLoweringError
        from zlang.simulation_request_response import lower_request_response_module

        try:
            module = lower_request_response_module(module)
        except ProtocolSimulationLoweringError as error:
            raise JitUnsupportedFeatureError(str(error)) from error

    if module.elaborated_instances or module.instances or module.children:
        from zlang.simulation_hierarchy import (
            HierarchicalSimulationError,
            compose_hierarchical_primitive_payload,
        )
        from zlang.simulation_protocol_hierarchy import lower_protocol_hierarchy
        from zlang.simulation_protocol_shared import ProtocolSimulationLoweringError

        try:
            module = lower_protocol_hierarchy(module)
            payload = compose_hierarchical_primitive_payload(
                module,
                lambda child: _build_leaf_simulation_plan(child, policy=policy),
                max_nodes=max_nodes,
            )
        except (HierarchicalSimulationError, ProtocolSimulationLoweringError) as error:
            raise JitUnsupportedFeatureError(str(error)) from error
        canonical_bytes, identity = identity_bytes(payload)
        if len(canonical_bytes) > max_bytes:
            raise SimulationPlanError(
                f"simulation plan exceeds {max_bytes} encoded bytes"
            )
        payload["identity"] = identity
        _validate_plan_payload(payload, policy=policy)
        return SimulationPlan(payload, canonical_bytes, identity)
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        from zlang.simulation_protocol_pipeline import lower_protocol_module
        from zlang.simulation_protocol_shared import ProtocolSimulationLoweringError

        try:
            module = lower_protocol_module(module)
        except ProtocolSimulationLoweringError as error:
            raise JitUnsupportedFeatureError(str(error)) from error
    return _build_leaf_simulation_plan(module, policy=policy)



def _build_leaf_simulation_plan(
    module: object,
    *,
    policy: SimulationPlanPolicy = DEFAULT_SIMULATION_PLAN_POLICY,
) -> SimulationPlan:
    """Build one hierarchy-free executable primitive plan."""

    max_bytes = policy.max_bytes
    max_events = policy.max_events
    max_memory_bits = policy.max_memory_bits
    max_memory_width = policy.max_memory_width
    max_nodes = policy.max_nodes
    max_width = policy.max_width

    from zlang.simulation_csr import (
        CsrSimulationLoweringError,
        lower_csr_module,
    )
    from zlang.simulation_external import (
        ExternalModelSimulationLoweringError,
        lower_external_model,
    )

    try:
        module = lower_csr_module(module)
        module = lower_external_model(
            module,
            max_nodes=max_nodes,
        )
    except (
        CsrSimulationLoweringError,
        ExternalModelSimulationLoweringError,
    ) as error:
        raise JitUnsupportedFeatureError(str(error)) from error
    module = _lower_direct_wire_connections(module)
    # Canonical IR intentionally retains callable definitions for provenance
    # and backend emission.  Primitive simulation has no callable operation,
    # so remove definitions that are unreachable from this hierarchy shell
    # and perform the existing exact bounded inlining before serializing its
    # executable DAG.
    from zlang.ir.normalization import (
        SelectedValueNormalizationError,
        normalize_selected_values,
    )

    try:
        module = normalize_selected_values(
            module,
            # The normalizer charges each call body before shared-DAG
            # canonicalization.  Allow a bounded 2x construction allowance;
            # the exact post-lowering plan still must satisfy MAX_PLAN_NODES.
            total_inline_nodes=max_nodes * 2,
            inline_all_calls=True,
            expand_exact_reductions=True,
        )
    except SelectedValueNormalizationError as error:
        raise JitUnsupportedFeatureError(str(error)) from error
    module = _erase_non_runtime_metadata(module)

    unsupported = _unsupported_module_features(module)
    if unsupported:
        raise JitUnsupportedFeatureError(
            "native simulation does not yet support: " + ", ".join(unsupported)
        )
    canonical = lower(module, stage=OptimizationStage.SELECTED_ARCHITECTURE)
    if len(canonical.expressions) > max_nodes:
        raise SimulationPlanError(
            f"simulation plan exceeds {max_nodes} expression nodes"
        )

    expression_plan = PrimitiveExpressionPlanBuilder(
        module,
        canonical,
        max_memory_width,
        max_memory_bits,
        max_width,
    ).build()

    # Verification remains an execution overlay with its own canonical DAG so
    # it cannot perturb hardware/candidate identity.
    verification_overlay = VerificationOverlayBuilder(
        canonical,
        expression_plan.nodes,
        max_events,
        max_nodes,
        max_width,
    ).build()

    runtime_state = RuntimeStatePlanBuilder(
        canonical,
        expression_plan.nodes,
        expression_plan.staged_expressions,
        expression_plan.rom_result_names,
        expression_plan.packed_register_initials,
    ).build()
    storage_plan = StoragePlanBuilder(
        module,
        canonical,
        expression_plan.nodes,
        runtime_state.registers,
        runtime_state.domains,
        expression_plan.fifo_state,
        expression_plan.memory_read_registers,
    ).build()
    if len(expression_plan.nodes) > max_nodes:
        raise SimulationPlanError(
            f"simulation plan exceeds {max_nodes} expression nodes after "
            "sequential-state materialization"
        )

    transition_plan = TransitionPlanBuilder(canonical).build()

    payload: dict[str, Any] = {
        "schema": policy.schema,
        "runtime_abi": policy.runtime_abi,
        "packing_layout_schema": PACKING_LAYOUT_SCHEMA,
        "canonical_ir_identity": canonical_ir_identity(canonical),
        "native_target": {
            "triple": f"{platform.machine().lower()}-unknown-{platform.system().lower()}-gnu",
            "cranelift": policy.cranelift_version,
            "isa_flags": ["native"],
        },
        "module": canonical.name,
        "identity": "",
        "ports": runtime_state.ports,
        "nodes": expression_plan.nodes,
        "outputs": runtime_state.outputs,
        "registers": runtime_state.registers,
        "memories": storage_plan.memories,
        "events": verification_overlay.events,
        "instrumentation_scopes": verification_overlay.instrumentation_scopes,
        "fifos": storage_plan.fifos,
        "domains": runtime_state.domains,
        "direct_next": runtime_state.direct_next,
        "transitions": transition_plan.transitions,
        "scheduler_fifos": transition_plan.scheduler_fifos,
        "scheduler_guards": transition_plan.scheduler_guards,
        "scheduler_activations": transition_plan.scheduler_activations,
    }
    try:
        payload = simulation_lowering.lower_to_primitive_plan(
            payload,
            max_nodes=max_nodes,
        )
    except simulation_lowering.PrimitiveLoweringError as error:
        raise SimulationPlanError(str(error)) from error
    canonical_bytes, identity = identity_bytes(payload)
    if len(canonical_bytes) > max_bytes:
        raise SimulationPlanError(
            f"simulation plan exceeds {max_bytes} encoded bytes"
        )
    payload["identity"] = identity
    _validate_plan_payload(payload, policy=policy)
    return SimulationPlan(payload, canonical_bytes, identity)
