"""Mechanical adapter between typed canonical IR and an e-graph-facing IR.

No rewrite rules or extraction policy live here. This boundary carries only
pure scalar value subgraphs and preserves typed metadata and source origins.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Any

from zlang.ir.type_codec import (
    TypeCodecError,
    scalar_type_data,
    scalar_type_from_data,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)
from zlang.ir.expressions import BinaryOperator, ReductionOperator
from zlang.opt.ir import (
    CanonicalExpression, CanonicalModule, EffectKind, ExpressionOp, NodeCategory,
    NodeId, NodeMetadata, Purity, Signedness,
)
from zlang.opt.capabilities import expression_capability
from zlang.opt.lowering import restore_expression
from zlang.source import SourceOrigin


class EGraphAdapterError(ValueError):
    """The requested canonical subgraph is outside the adapter boundary."""


@dataclass(frozen=True)
class EGraphNode:
    id: int
    category: NodeCategory
    op: ExpressionOp
    type: HardwareType
    metadata: NodeMetadata
    operands: tuple[int, ...] = ()
    attributes: tuple[tuple[str, object], ...] = ()
    origins: tuple[SourceOrigin, ...] = ()

    @classmethod
    def from_canonical(
        cls,
        node: CanonicalExpression,
        *,
        identity: int,
        operands: tuple[int, ...],
    ) -> EGraphNode:
        return cls(
            identity,
            node.category,
            node.op,
            node.type,
            node.metadata,
            operands,
            node.attributes,
            node.origins,
        )

    def to_canonical(self) -> CanonicalExpression:
        return CanonicalExpression(
            self.id,
            self.category,
            self.op,
            self.type,
            self.metadata,
            self.operands,
            self.attributes,
            self.origins,
        )


@dataclass(frozen=True)
class EGraphProgram:
    root: int
    nodes: tuple[EGraphNode, ...]

    def __post_init__(self) -> None:
        if not self.nodes or not 0 <= self.root < len(self.nodes):
            raise EGraphAdapterError("an e-graph program needs a valid root")
        for expected, node in enumerate(self.nodes):
            if node.id != expected:
                raise EGraphAdapterError("e-graph node IDs must be contiguous")
            if any(operand < 0 or operand >= node.id for operand in node.operands):
                raise EGraphAdapterError(f"e-graph node %{node.id} has a non-prior operand")
            if node.category is not NodeCategory.VALUE:
                raise EGraphAdapterError("e-graph nodes must be value nodes")
            if node.metadata.purity is not Purity.PURE:
                raise EGraphAdapterError("e-graph nodes must be pure")


def canonical_to_egraph(module: CanonicalModule, root: NodeId) -> EGraphProgram:
    """Copy one eligible canonical value subgraph into the adapter boundary."""
    return canonical_nodes_to_egraph(module.expressions, root)


def canonical_nodes_to_egraph(
    source: tuple[CanonicalExpression, ...],
    root: NodeId,
) -> EGraphProgram:
    """Copy an eligible canonical expression graph into the adapter boundary.

    This entry point allows a consumer to perform a bounded semantic operation,
    such as expansion of retained typed calls, before applying the unchanged
    frozen M26 eligibility checks.
    """

    validate_scalar_pure_nodes(source, root)
    order: list[NodeId] = []
    seen: set[NodeId] = set()

    def visit(node_id: NodeId) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        for operand in source[node_id].operands:
            visit(operand)
        order.append(node_id)

    visit(root)
    remap = {old: new for new, old in enumerate(order)}
    return EGraphProgram(
        remap[root],
        tuple(
            EGraphNode.from_canonical(
                source[old],
                identity=remap[old],
                operands=tuple(remap[item] for item in source[old].operands),
            )
            for old in order
        ),
    )


def egraph_to_canonical(program: EGraphProgram) -> tuple[tuple[CanonicalExpression, ...], int]:
    """Restore an e-graph program to canonical expression nodes and root."""
    return (
        tuple(node.to_canonical() for node in program.nodes),
        program.root,
    )


def egraph_to_expression(program: EGraphProgram):
    """Restore the root as the existing typed semantic expression IR."""
    nodes, root = egraph_to_canonical(program)
    return restore_expression(nodes, root)


def serialize_egraph(program: EGraphProgram) -> str:
    """Serialize an e-graph program as deterministic, versioned JSON."""
    payload = {
        "schema": "zlang-egraph-v1", "root": program.root,
        "nodes": [
            {
                "id": node.id, "category": node.category.value, "op": node.op.value,
                "type": _encode_type(node.type), "metadata": _encode_metadata(node.metadata),
                "operands": list(node.operands),
                "attributes": [[name, _encode_value(value)] for name, value in node.attributes],
                "origins": [_encode_origin(origin) for origin in node.origins],
            }
            for node in program.nodes
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def deserialize_egraph(text: str) -> EGraphProgram:
    """Parse and validate a serialized e-graph program."""
    try:
        payload = json.loads(text)
        if payload.get("schema") != "zlang-egraph-v1":
            raise EGraphAdapterError("unsupported e-graph serialization schema")
        nodes = tuple(
            EGraphNode(
                item["id"], NodeCategory(item["category"]), ExpressionOp(item["op"]),
                _decode_type(item["type"]), _decode_metadata(item["metadata"]),
                tuple(item["operands"]),
                tuple((name, _decode_value(value)) for name, value in item["attributes"]),
                tuple(_decode_origin(origin) for origin in item["origins"]),
            )
            for item in payload["nodes"]
        )
        return EGraphProgram(payload["root"], nodes)
    except EGraphAdapterError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise EGraphAdapterError(f"invalid e-graph serialization: {error}") from error


def render_egraph(program: EGraphProgram) -> str:
    """Render a concise deterministic debug dump."""
    lines = [f"egraph root=%{program.root} nodes={len(program.nodes)}"]
    for node in program.nodes:
        operands = " ".join(f"%{item}" for item in node.operands)
        origins = ",".join(origin.render() for origin in node.origins) or "none"
        lines.append(
            f"  %{node.id} {node.op.value} type={node.type} width={node.metadata.width} "
            f"latency={node.metadata.latency} ii={node.metadata.initiation_interval} "
            f"origins=[{origins}]" + (f" operands={operands}" if operands else "")
        )
    return "\n".join(lines) + "\n"


def _require_scalar_pure_root(module: CanonicalModule, root: NodeId) -> None:
    validate_scalar_pure_nodes(module.expressions, root)


def validate_scalar_pure_nodes(
    expressions: tuple[CanonicalExpression, ...],
    root: NodeId,
    *,
    allow_retained_calls: bool = False,
) -> None:
    """Validate the frozen exact-scalar e-graph boundary.

    Retained calls are admitted only for the bounded pre-expansion check used
    by saturation. Programs copied into the adapter always use the strict
    default and therefore never contain calls.
    """
    if root < 0 or root >= len(expressions):
        raise EGraphAdapterError(f"canonical expression root %{root} does not exist")
    seen: set[NodeId] = set()

    def visit(node_id: NodeId) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        node = expressions[node_id]
        if node.category is not NodeCategory.VALUE or node.metadata.purity is not Purity.PURE:
            effects = ",".join(effect.value for effect in node.metadata.effects) or "none"
            raise EGraphAdapterError(
                f"root %{root} is not a pure mathematical value: dependency "
                f"%{node.id} is {node.category.value}.{node.op.value} "
                f"purity={node.metadata.purity.value} effects=[{effects}]"
            )
        if not isinstance(
            node.type,
            (BitType, UIntType, SIntType, BitsType, FixedType, UFixedType),
        ):
            raise EGraphAdapterError("e-graph currently supports scalar hardware types only")
        capability = expression_capability(node.op)
        if (
            capability is None or not capability.egraph_exact
        ) and not (allow_retained_calls and node.op is ExpressionOp.CALL):
            raise EGraphAdapterError(
                f"{node.op.value} is outside the exact scalar e-graph operation set"
            )
        operand_types = tuple(expressions[item].type for item in node.operands)
        if node.op is ExpressionOp.BINARY:
            operator = node.attribute("operator")
            if operator in {BinaryOperator.SHIFT_LEFT, BinaryOperator.SHIFT_RIGHT}:
                compatible = bool(operand_types) and operand_types[0] == node.type
            elif operator in {
                BinaryOperator.BIT_AND,
                BinaryOperator.BIT_OR,
                BinaryOperator.BIT_XOR,
            }:
                compatible = all(item == node.type for item in operand_types)
            else:
                compatible = True
            if not compatible:
                raise EGraphAdapterError(
                    f"root %{root} has incompatible operand types for {operator.value}"
                )
        elif node.op is ExpressionOp.MUX:
            compatible = (
                len(operand_types) == 3
                and isinstance(operand_types[0], BitType)
                and operand_types[1] == node.type
                and operand_types[2] == node.type
            )
            if not compatible:
                raise EGraphAdapterError(
                    f"root %{root} has incompatible mux operand types"
                )
        for operand in node.operands:
            visit(operand)

    visit(root)


def _encode_type(type_: HardwareType) -> dict[str, Any]:
    try:
        return scalar_type_data(type_)
    except TypeCodecError as error:
        raise EGraphAdapterError(str(error)) from error


def _decode_type(payload: dict[str, Any]) -> HardwareType:
    try:
        return scalar_type_from_data(payload)
    except TypeCodecError as error:
        raise EGraphAdapterError(str(error)) from error


def _encode_metadata(metadata: NodeMetadata) -> dict[str, Any]:
    return {"width": metadata.width, "signedness": metadata.signedness.value,
            "latency": metadata.latency, "initiation_interval": metadata.initiation_interval,
            "domains": list(metadata.domains), "purity": metadata.purity.value,
            "effects": [effect.value for effect in metadata.effects]}


def _decode_metadata(payload: dict[str, Any]) -> NodeMetadata:
    return NodeMetadata(payload["width"], Signedness(payload["signedness"]), payload["latency"],
                        payload["initiation_interval"], tuple(payload["domains"]),
                        Purity(payload["purity"]), tuple(EffectKind(item) for item in payload["effects"]))


def _encode_origin(origin: SourceOrigin) -> dict[str, Any]:
    return origin.to_data()


def _decode_origin(payload: dict[str, Any]) -> SourceOrigin:
    return SourceOrigin.from_data(payload)


def _encode_value(value: object) -> object:
    if isinstance(value, Enum):
        return {"enum": value.__class__.__name__, "value": value.value}
    if isinstance(value, HardwareType):
        try:
            return {"type": _encode_type(value)}
        except EGraphAdapterError:
            pass
    if isinstance(value, tuple):
        return {"tuple": [_encode_value(item) for item in value]}
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise EGraphAdapterError(f"unsupported e-graph attribute value {value!r}")


_ENUMS = {
    enum.__name__: enum
    for enum in (
        NodeCategory, ExpressionOp, EffectKind, Purity, Signedness,
        BinaryOperator, ReductionOperator,
    )
}


def _decode_value(value: object) -> object:
    if isinstance(value, dict) and "enum" in value:
        try:
            return _ENUMS[value["enum"]](value["value"])
        except (KeyError, ValueError) as error:
            raise EGraphAdapterError(
                f"unsupported serialized enum {value!r}"
            ) from error
    if isinstance(value, dict) and "type" in value:
        return _decode_type(value["type"])
    if isinstance(value, dict) and "tuple" in value:
        return tuple(_decode_value(item) for item in value["tuple"])
    return value
