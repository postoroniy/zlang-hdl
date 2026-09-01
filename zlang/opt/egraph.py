"""Mechanical adapter between typed canonical IR and an e-graph-facing IR.

No rewrite rules or extraction policy live here. This boundary carries only
pure scalar value subgraphs and preserves typed metadata and source origins.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Any

from zlang.ir.types import BitType, BitsType, HardwareType, SIntType, UIntType
from zlang.ir.expressions import BinaryOperator, ReductionOperator
from zlang.opt.ir import (
    CanonicalExpression, CanonicalModule, EffectKind, ExpressionOp, NodeCategory,
    NodeId, NodeMetadata, Purity, Signedness,
)
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

    _require_scalar_pure_nodes(source, root)
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
            EGraphNode(
                remap[old], source[old].category, source[old].op, source[old].type,
                source[old].metadata,
                tuple(remap[item] for item in source[old].operands),
                source[old].attributes, source[old].origins,
            )
            for old in order
        ),
    )


def egraph_to_canonical(program: EGraphProgram) -> tuple[tuple[CanonicalExpression, ...], int]:
    """Restore an e-graph program to canonical expression nodes and root."""
    return (
        tuple(
            CanonicalExpression(
                node.id, node.category, node.op, node.type, node.metadata,
                node.operands, node.attributes, node.origins,
            )
            for node in program.nodes
        ),
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
    _require_scalar_pure_nodes(module.expressions, root)


def _require_scalar_pure_nodes(
    expressions: tuple[CanonicalExpression, ...],
    root: NodeId,
) -> None:
    if root < 0 or root >= len(expressions):
        raise EGraphAdapterError(f"canonical expression root %{root} does not exist")
    seen: set[NodeId] = set()

    def visit(node_id: NodeId) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        node = expressions[node_id]
        if node.category is not NodeCategory.VALUE or node.metadata.purity is not Purity.PURE:
            raise EGraphAdapterError(f"dependency %{node_id} is outside the pure value e-graph boundary")
        if node.op in {
            ExpressionOp.SLICE,
            ExpressionOp.CONCAT,
            ExpressionOp.BITCAST,
            ExpressionOp.VECTOR_CONCAT,
            ExpressionOp.RESHAPE,
            ExpressionOp.PACK,
            ExpressionOp.UNPACK,
        }:
            raise EGraphAdapterError(
                f"{node.op.value} is outside the frozen scalar e-graph operation set"
            )
        if not isinstance(node.type, (BitType, UIntType, SIntType, BitsType)):
            raise EGraphAdapterError("e-graph currently supports scalar hardware types only")
        for operand in node.operands:
            visit(operand)

    visit(root)


def _encode_type(type_: HardwareType) -> dict[str, Any]:
    if isinstance(type_, BitType): return {"kind": "bit"}
    if isinstance(type_, UIntType): return {"kind": "uint", "width": type_.width}
    if isinstance(type_, SIntType): return {"kind": "sint", "width": type_.width}
    if isinstance(type_, BitsType): return {"kind": "bits", "width": type_.width}
    raise EGraphAdapterError("only scalar hardware types can cross the e-graph boundary")


def _decode_type(payload: dict[str, Any]) -> HardwareType:
    kind = payload["kind"]
    if kind == "bit": return BitType()
    if kind == "uint": return UIntType(payload["width"])
    if kind == "sint": return SIntType(payload["width"])
    if kind == "bits": return BitsType(payload["width"])
    raise EGraphAdapterError(f"unknown scalar type '{kind}'")


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
    if isinstance(value, Enum): return {"enum": value.__class__.__name__, "value": value.value}
    if isinstance(value, (BitType, UIntType, SIntType, BitsType)): return {"type": _encode_type(value)}
    if isinstance(value, tuple): return {"tuple": [_encode_value(item) for item in value]}
    if isinstance(value, (str, int, bool)) or value is None: return value
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
        try: return _ENUMS[value["enum"]](value["value"])
        except (KeyError, ValueError) as error: raise EGraphAdapterError(f"unsupported serialized enum {value!r}") from error
    if isinstance(value, dict) and "type" in value: return _decode_type(value["type"])
    if isinstance(value, dict) and "tuple" in value: return tuple(_decode_value(item) for item in value["tuple"])
    return value
