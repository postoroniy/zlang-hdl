"""Typed relation descriptor for bounded capacity-one temporal streams.

This is intentionally not a general temporal prover.  It owns the exact
capacity-one relation and a bounded backend miter for it, without
misrepresenting either as the fixed-latency scalar equivalence route.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from zlang.common.serialization import stable_digest
from zlang.ir.signed_reductions import expression_semantic_identity
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


__all__ = [
    "CapacityOneTransactionRelation",
    "CapacityOneTransactionMiter",
    "build_capacity_one_transaction_relation",
    "emit_capacity_one_transaction_miter",
]
