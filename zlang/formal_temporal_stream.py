"""Typed relation descriptor for bounded capacity-one temporal streams.

This is intentionally not a general temporal prover.  It owns the exact
capacity-one relation and a bounded backend miter for it, without
misrepresenting either as the fixed-latency scalar equivalence route.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zlang.backend.manifest import (
    MANIFEST_VERSION,
    BackendArtifact,
    backend_binding_identity,
)
from zlang.backend.naming import RTL_NAMING_SCHEMA
from zlang.backend.systemverilog import emit_formal_artifact
from zlang.backend.systemverilog.expression import render_expression
from zlang.common.serialization import stable_digest
from zlang.costs import CandidateCost, extract_best
from zlang.formal import (
    build_recursive_formal_design,
    run_verilog_formal,
    use_formal_toolchain,
)
from zlang.formal_artifact_provider import (
    FormalArtifactNamespace,
    FormalArtifactProvider,
)
from zlang.formal_exploration import (
    FormalExplorationConfig,
    FormalExplorationError,
    FormalGateResult,
    FormalPolicy,
    gate_candidates,
)
from zlang.ir.cdc import clock_domain_contract_identity, clock_domain_data
from zlang.ir.expressions import CostMetric
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.ir.module import Module, dependency_context_identity
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.temporal import TemporalClass
from zlang.ir.temporal_admission import TemporalAdmissionPolicy
from zlang.ir.types import SIntType, StructType


@dataclass(frozen=True)
class CapacityOneTransactionRelation:
    """The transaction-indexed obligations of one non-interleaved region."""

    semantic_region_identity: str
    implementation_identity: str
    source_endpoint: str
    destination_endpoint: str
    expression_identity: str
    capacity: int
    property_identity: str

    def __post_init__(self) -> None:
        if not all((
            self.semantic_region_identity,
            self.implementation_identity,
            self.source_endpoint,
            self.destination_endpoint,
            self.expression_identity,
            self.property_identity,
        )):
            raise ValueError("temporal stream relation identities must be non-empty")
        if self.capacity != 1:
            raise ValueError("only capacity-one temporal stream relations are supported")


@dataclass(frozen=True)
class CapacityOneTransactionMiter:
    """One backend-bound BMC harness for the exact capacity-one relation."""

    relation: CapacityOneTransactionRelation
    top: str
    source: str
    implementation_artifact_hash: str

    def __post_init__(self) -> None:
        if not self.top or not self.source or not self.implementation_artifact_hash:
            raise ValueError("temporal stream miter is incomplete")


@dataclass(frozen=True)
class CapacityOneTemporalCandidate:
    """One already-selected temporal implementation presented to the formal gate."""

    module: Module
    region: object
    semantic_identity: str
    implementation_identity: str
    cost: CandidateCost


@dataclass(frozen=True)
class PreparedCapacityOneTransactionProof:
    """Backend-bound transaction miter and its exact cache identity inputs."""

    relation: CapacityOneTransactionRelation
    miter: CapacityOneTransactionMiter
    artifact: BackendArtifact
    reference_artifact_hash: str
    harness_hash: str
    assumptions_identity: str
    backend_identity: str
    reset_contract_identity: str


def build_capacity_one_transaction_relation(region: object) -> CapacityOneTransactionRelation:
    """Build the exact relation for a supported retire-and-reload region.

    Its bounded miter establishes ordered payload equality for every
    accepted/retired transaction and `input_count - output_count <= 1`; this
    record is deliberately the immutable compiler-owned identity for that
    relation, not a textual SVA approximation.
    """

    graph = getattr(region, "temporal_graph", None)
    if graph is None or graph.capacity != 1:
        raise ValueError("temporal stream relation requires a capacity-one graph")
    if graph.admission_policy is not TemporalAdmissionPolicy.RETIRE_AND_RELOAD:
        raise ValueError("temporal stream relation requires retire-and-reload admission")
    if (
        graph.temporal_class is not TemporalClass.FLOW_CONTROLLED
        or graph.latency != 4
        or graph.initiation_interval != 4
    ):
        raise ValueError(
            "temporal stream relation requires the bounded latency-4 II-4 flow-controlled graph"
        )
    operation_shape = tuple(
        (
            item.operation_id,
            item.operation,
            item.start_cycle,
            item.result_cycle,
            item.resource_class,
        )
        for item in graph.operations
    )
    if operation_shape != (
        ("mul0", "multiply", 1, 1, "multiply"),
        ("mul1", "multiply", 2, 2, "multiply"),
        ("add0", "add", 3, 3, "add"),
    ):
        raise ValueError("temporal stream relation supports only the two-product shared schedule")
    resource_shape = tuple(
        (item.operation_id, item.physical_resource_id)
        for item in graph.resource_bindings
    )
    if resource_shape != (
        ("mul0", "multiply0"),
        ("mul1", "multiply0"),
        ("add0", "add0"),
    ):
        raise ValueError("temporal stream relation requires exactly one shared multiplier")
    expression = getattr(region, "source_expression")
    semantic_id = getattr(region, "semantic_id")
    source = getattr(region, "source_endpoint")
    destination = getattr(region, "destination_endpoint")
    expression_id = expression_semantic_identity(expression)
    identity = "temporal-stream:" + stable_digest({
        "schema": "zlang-capacity-one-transaction-relation-v1",
        "semantic_region_identity": semantic_id,
        "implementation_identity": graph.implementation_identity,
        "source": source,
        "destination": destination,
        "expression": expression_id,
        "capacity": 1,
        "admission": graph.admission_policy.value,
    })
    return CapacityOneTransactionRelation(
        semantic_region_identity=semantic_id,
        implementation_identity=graph.implementation_identity,
        source_endpoint=source,
        destination_endpoint=destination,
        expression_identity=expression_id,
        capacity=1,
        property_identity=identity,
    )


def _supported_clock_domain(module: Module, region: object):
    matches = tuple(
        item
        for item in module.clock_domains
        if item.clock == getattr(region, "clock")
        and item.reset == getattr(region, "reset")
    )
    if len(matches) != 1 or len(module.clock_domains) != 1:
        raise ValueError(
            "temporal transaction BMC requires one exact clock/reset domain"
        )
    domain = matches[0]
    if not domain.is_legacy_default:
        raise ValueError(
            "temporal transaction BMC supports only rising-edge synchronous "
            "active-high reset with unspecified power-up"
        )
    return domain


def _binding_path(artifact: object, semantic_id: str) -> object:
    """Resolve one compiler-published physical binding, never a guessed name."""

    bindings = tuple(getattr(artifact, "bindings", ()))
    matches = tuple(
        item for item in bindings
        if getattr(item, "semantic_signal_id", None) == semantic_id
        and getattr(item, "physical_available", False)
        and getattr(item, "rtl_path", "")
    )
    if len(matches) != 1:
        raise ValueError(
            f"temporal stream miter requires one published binding for '{semantic_id}'"
        )
    return matches[0]


def _sv_type(binding: object) -> str:
    width = getattr(binding, "width", None)
    signedness = getattr(binding, "signedness", None)
    if not isinstance(width, int) or width < 1:
        raise ValueError("temporal stream miter binding width is invalid")
    signed = " signed" if signedness == "signed" else ""
    return f"logic{signed}" if width == 1 else f"logic{signed} [{width - 1}:0]"


def emit_capacity_one_transaction_miter(
    region: object,
    *,
    artifact: object,
    render_expression: Callable[[object], str],
) -> CapacityOneTransactionMiter:
    """Emit a small transaction-indexed miter for the exact supported shape.

    ``artifact.bindings`` is the only source of physical port names.  The
    typed source expression is rendered by the caller's normal Direct-SV
    renderer, so this miter never reconstructs arithmetic width or
    signedness rules from source text.
    """

    relation = build_capacity_one_transaction_relation(region)
    source = relation.source_endpoint
    destination = relation.destination_endpoint
    input_type = getattr(region, "input_type")
    output_type = getattr(region, "output_type")
    if not isinstance(input_type, StructType):
        raise ValueError("temporal stream miter requires a struct ready/valid payload")
    if not all(field.type.width >= 1 for field in input_type.fields):
        raise ValueError("temporal stream miter payload fields are invalid")

    clock = _binding_path(artifact, "clock")
    reset = _binding_path(artifact, "reset")
    input_valid = _binding_path(artifact, f"port:{source}.valid")
    input_ready = _binding_path(artifact, f"port:{source}.ready")
    output_payload = _binding_path(artifact, f"port:{destination}.payload")
    output_valid = _binding_path(artifact, f"port:{destination}.valid")
    output_ready = _binding_path(artifact, f"port:{destination}.ready")
    payload_fields = tuple(
        (field, _binding_path(artifact, f"port:{source}.payload.{field.name}"))
        for field in input_type.fields
    )
    paths = {
        getattr(item, "rtl_path")
        for item in (
            clock, reset, input_valid, input_ready, output_payload,
            output_valid, output_ready, *(item for _, item in payload_fields),
        )
    }
    if any(not isinstance(path, str) or not path.isidentifier() for path in paths):
        raise ValueError("temporal stream miter requires identifier-shaped top bindings")

    packed_input = f"zlang_{source}_payload"
    expected = "zlang_temporal_expected"
    outstanding = "zlang_temporal_outstanding"
    stalled = "zlang_temporal_stalled"
    prior_payload = "zlang_temporal_prior_payload"
    top = "temporal_stream_miter_" + hashlib.sha256(
        relation.property_identity.encode()
    ).hexdigest()[:16]
    expression = render_expression(getattr(region, "source_expression"))
    output_signed = " signed" if isinstance(output_type, SIntType) else ""
    clock_path = getattr(clock, "rtl_path")
    reset_path = getattr(reset, "rtl_path")
    input_valid_path = getattr(input_valid, "rtl_path")
    input_ready_path = getattr(input_ready, "rtl_path")
    output_payload_path = getattr(output_payload, "rtl_path")
    output_valid_path = getattr(output_valid, "rtl_path")
    output_ready_path = getattr(output_ready, "rtl_path")
    lines = [
        getattr(artifact, "text"),
        "`default_nettype none",
        f"module {top}(input logic {clock_path}, input logic {reset_path});",
    ]
    lines.extend(
        f"  (* anyseq *) {_sv_type(binding)} {getattr(binding, 'rtl_path')};"
        for _, binding in payload_fields
    )
    lines.extend((
        f"  (* anyseq *) logic {input_valid_path};",
        f"  (* anyseq *) logic {output_ready_path};",
        f"  logic {input_ready_path};",
        f"  logic{output_signed} [{output_type.width - 1}:0] {output_payload_path};",
        f"  logic {output_valid_path};",
        f"  logic [{input_type.width - 1}:0] {packed_input};",
        f"  logic{output_signed} [{output_type.width - 1}:0] {expected};",
        f"  logic {outstanding};",
        f"  logic {stalled};",
        f"  logic{output_signed} [{output_type.width - 1}:0] {prior_payload};",
        # The compiler-supported reset contract requires a reset epoch before
        # transactions.  Subsequent reset pulses remain unconstrained and are
        # checked by the synchronous reset branch below.
        f"  initial assume ({reset_path});",
        "  assign " + packed_input + " = {"
        + ", ".join(getattr(binding, "rtl_path") for _, binding in payload_fields)
        + "};",
        f"  wire zlang_temporal_input_transfer = {input_valid_path} && {input_ready_path};",
        f"  wire zlang_temporal_output_transfer = {output_valid_path} && {output_ready_path};",
        f"  {getattr(artifact, 'module')} implementation_i(",
        "    ." + clock_path + "(" + clock_path + "),",
        "    ." + reset_path + "(" + reset_path + "),",
        *(
            "    ." + getattr(binding, "rtl_path") + "("
            + getattr(binding, "rtl_path") + "),"
            for _, binding in payload_fields
        ),
        f"    .{input_valid_path}({input_valid_path}),",
        f"    .{input_ready_path}({input_ready_path}),",
        f"    .{output_payload_path}({output_payload_path}),",
        f"    .{output_valid_path}({output_valid_path}),",
        f"    .{output_ready_path}({output_ready_path})",
        "  );",
        f"  always_ff @(posedge {clock_path}) begin",
        f"    if ({reset_path}) begin",
        f"      {outstanding} <= 1'b0;",
        f"      {expected} <= '0;",
        f"      {stalled} <= 1'b0;",
        f"      {prior_payload} <= '0;",
        "    end else begin",
        f"      if ({stalled}) begin",
        f"        assert ({output_valid_path});",
        f"        assert ({output_payload_path} == {prior_payload});",
        "      end",
        "      if (zlang_temporal_output_transfer) begin",
        f"        assert ({outstanding});",
        f"        assert ({output_payload_path} == {expected});",
        "      end",
        "      if (zlang_temporal_input_transfer) begin",
        f"        if ({outstanding}) assert (zlang_temporal_output_transfer);",
        f"        {outstanding} <= 1'b1;",
        f"        {expected} <= {expression};",
        "      end else if (zlang_temporal_output_transfer) begin",
        f"        {outstanding} <= 1'b0;",
        "      end",
        f"      {stalled} <= {output_valid_path} && !{output_ready_path};",
        f"      {prior_payload} <= {output_payload_path};",
        "    end",
        "  end",
        "endmodule",
        "`default_nettype wire",
        "",
    ))
    return CapacityOneTransactionMiter(
        relation=relation,
        top=top,
        source="\n".join(lines),
        implementation_artifact_hash=getattr(artifact, "artifact_hash"),
    )


class CapacityOneTransactionVerifier:
    """Execute only the bounded capacity-one transaction-stream relation.

    The value e-graph and temporal scheduler have already done their jobs when
    this owner is constructed.  It binds the frozen relation through one
    BackendArtifact, runs BMC, and returns the normal formal-aware selection
    result shape.  It deliberately has no unbounded proof mode.
    """

    formal_route = "transaction_stream_equivalence_bmc"

    def __init__(
        self,
        module: Module,
        region: object,
        *,
        artifact_provider: FormalArtifactProvider | None = None,
    ) -> None:
        self.module = module
        self.region = region
        self.relation = build_capacity_one_transaction_relation(region)
        self.domain = _supported_clock_domain(module, region)
        self.artifact_provider = artifact_provider or FormalArtifactProvider()

    def preparation_cache_recipe(
        self,
        _candidate: object,
        config: FormalExplorationConfig,
    ) -> dict[str, object]:
        return {
            "schema": "zlang-capacity-one-transaction-proof-bundle-v1",
            "relation": self.relation.property_identity,
            "semantic_region": self.relation.semantic_region_identity,
            "implementation": self.relation.implementation_identity,
            "expression": self.relation.expression_identity,
            "reset_contract": clock_domain_data(self.domain),
            "dependency": (
                config.dependency_identity
                or dependency_context_identity(self.module)
            ),
            "rtl_naming": RTL_NAMING_SCHEMA,
            "manifest_version": MANIFEST_VERSION,
            "backend": "direct_systemverilog",
        }

    def prepare(
        self,
        candidate: object,
        config: FormalExplorationConfig,
    ) -> PreparedCapacityOneTransactionProof:
        if config.engine != "sby":
            raise FormalExplorationError(
                "temporal transaction BMC supports only the configured 'sby' engine"
            )
        recipe = self.preparation_cache_recipe(candidate, config)
        return self.artifact_provider.get_or_prepare(
            FormalArtifactNamespace.FORMAL_SELECTION,
            "capacity-one-transaction-proof-preparation-v1",
            recipe,
            self._prepare,
            fingerprint=lambda value: {
                "property_identity": value.relation.property_identity,
                "artifact_hash": value.artifact.artifact_hash,
                "harness_hash": value.harness_hash,
                "assumptions_identity": value.assumptions_identity,
                "backend_identity": value.backend_identity,
            },
        )

    def _prepare(self) -> PreparedCapacityOneTransactionProof:
        selected_identity = self.relation.implementation_identity
        recursive = build_recursive_formal_design(
            self.module,
            selected_ir_identity=selected_identity,
        )
        artifact = emit_formal_artifact(
            self.module,
            recursive,
            selected_ir_identity=selected_identity,
        )
        miter = emit_capacity_one_transaction_miter(
            self.region,
            artifact=artifact,
            render_expression=render_expression,
        )
        reset_identity = clock_domain_contract_identity(self.domain)
        reference_hash = stable_digest({
            "schema": "zlang-capacity-one-transaction-reference-v1",
            "relation": self.relation.property_identity,
            "expression": self.relation.expression_identity,
        })
        harness_hash = hashlib.sha256(miter.source.encode()).hexdigest()
        assumptions_identity = stable_digest({
            "schema": "zlang-capacity-one-transaction-assumptions-v1",
            "relation": self.relation.property_identity,
            "capacity": 1,
            "latency": 4,
            "ii": 4,
            "admission": TemporalAdmissionPolicy.RETIRE_AND_RELOAD.value,
            "reset_contract": reset_identity,
        })
        backend_identity = stable_digest({
            "schema": "zlang-capacity-one-transaction-direct-sv-route-v1",
            "artifact_build": artifact.build_identity,
            "binding_identity": backend_binding_identity(artifact),
            "manifest_version": artifact.manifest_version,
            "rtl_naming": RTL_NAMING_SCHEMA,
        })
        return PreparedCapacityOneTransactionProof(
            self.relation,
            miter,
            artifact,
            reference_hash,
            harness_hash,
            assumptions_identity,
            backend_identity,
            reset_identity,
        )

    def cache_identity(
        self,
        candidate: object,
        config: FormalExplorationConfig,
    ) -> dict[str, str]:
        if config.policy is FormalPolicy.REQUIRED_PROVEN:
            return {
                "unavailable_reason": (
                    "capacity-one transaction-stream equivalence has bounded "
                    "BMC evidence only; unbounded proof is not supported"
                )
            }
        bundle = self.prepare(candidate, config)
        artifact_hash = bundle.artifact.artifact_hash
        return {
            "property_identity": bundle.relation.property_identity,
            "artifact_hash": artifact_hash,
            "reference_artifact_hash": bundle.reference_artifact_hash,
            "implementation_artifact_hash": artifact_hash,
            "harness_hash": bundle.harness_hash,
            "assumptions_identity": bundle.assumptions_identity,
            "backend_identity": bundle.backend_identity,
        }

    def __call__(
        self,
        candidate: object,
        config: FormalExplorationConfig,
    ) -> dict[str, object]:
        if config.policy is FormalPolicy.REQUIRED_PROVEN:
            return {
                "status": FormalStatus.SKIPPED,
                "mode": ProofMode.PROVE,
                "depth": config.bmc_depth,
                "engine": config.engine,
                "solver": config.solver,
                "backend": "direct_systemverilog",
                "reason": (
                    "capacity-one transaction-stream equivalence has bounded "
                    "BMC evidence only; unbounded proof is not supported"
                ),
            }
        bundle = self.prepare(candidate, config)
        resolver = config.tool_resolver
        resolve = getattr(resolver, "formal_context", None)
        context = (
            resolve(engine=config.engine, solver=config.solver)
            if callable(resolve)
            else None
        )
        manager = use_formal_toolchain(context) if context is not None else nullcontext()
        work_directory: Path | None = None
        if config.work_directory is not None:
            work_directory = (
                Path(config.work_directory).resolve(strict=False)
                / "formal_selection"
                / stable_digest({
                    "schema": "zlang-capacity-one-transaction-work-v1",
                    "relation": bundle.relation.property_identity,
                    "artifact": bundle.artifact.artifact_hash,
                    "harness": bundle.harness_hash,
                    "depth": config.bmc_depth,
                    "engine": config.engine,
                    "solver": config.solver,
                })
            )
        with manager:
            result = run_verilog_formal(
                bundle.miter.source,
                top=bundle.miter.top,
                property_id=bundle.relation.property_identity,
                mode=ProofMode.BMC,
                depth=config.bmc_depth,
                solver=config.solver,
                engine=config.engine,
                systemverilog=True,
                timeout_seconds=config.timeout_seconds,
                work_directory=work_directory,
            )
        artifact_hash = bundle.artifact.artifact_hash
        return {
            "status": result.status,
            "mode": ProofMode.BMC,
            "depth": result.depth or config.bmc_depth,
            "engine": result.engine or config.engine,
            "solver": result.solver or config.solver,
            "backend": "direct_systemverilog",
            "artifact_hash": artifact_hash,
            "implementation_artifact_hash": artifact_hash,
            "reference_artifact_hash": bundle.reference_artifact_hash,
            "property_identity": bundle.relation.property_identity,
            "harness_hash": bundle.harness_hash,
            "assumptions_identity": bundle.assumptions_identity,
            "backend_identity": bundle.backend_identity,
            "source_origin": getattr(self.region, "source_origin", None),
            "selected_origin": getattr(self.region, "source_origin", None),
            "work_directory": (
                None if work_directory is None else str(work_directory)
            ),
            "reason": result.reason or "",
            "counterexample": result.counterexample,
        }


def gate_capacity_one_transaction_region(
    module: Module,
    region: object,
    config: FormalExplorationConfig,
) -> FormalGateResult:
    """Run the existing formal-aware gate for one exact temporal candidate."""

    if config.policy is FormalPolicy.REQUIRED_PROVEN:
        raise FormalExplorationError(
            "required_proven is unavailable for capacity-one transaction-stream "
            "equivalence; only bounded BMC evidence is supported"
        )
    graph = getattr(region, "temporal_graph", None)
    if graph is None:
        raise FormalExplorationError("temporal transaction gate requires a temporal graph")
    candidate = CapacityOneTemporalCandidate(
        module=module,
        region=region,
        semantic_identity=getattr(region, "semantic_id"),
        implementation_identity=graph.implementation_identity,
        cost=CandidateCost.estimate(
            lut=graph.resource_cost.lut,
            ff=graph.resource_cost.ff,
            dsp=graph.resource_cost.dsp,
            latency=graph.latency,
            ii=graph.initiation_interval,
            structural_cost=len(graph.operations),
        ),
    )
    extraction = extract_best((candidate,), objective=CostMetric.LATENCY)
    verifier = CapacityOneTransactionVerifier(
        module,
        region,
        artifact_provider=(
            config.artifact_provider
            if isinstance(config.artifact_provider, FormalArtifactProvider)
            else None
        ),
    )
    return gate_candidates(
        (candidate,),
        extraction.evaluations,
        config,
        verifier,
    )


__all__ = [
    "CapacityOneTransactionRelation",
    "CapacityOneTransactionMiter",
    "CapacityOneTemporalCandidate",
    "CapacityOneTransactionVerifier",
    "PreparedCapacityOneTransactionProof",
    "build_capacity_one_transaction_relation",
    "emit_capacity_one_transaction_miter",
    "gate_capacity_one_transaction_region",
]
