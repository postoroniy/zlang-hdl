"""Canonical expression restoration into backend-independent semantic IR."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.functional import FunctionalLoweringError, vector_leaf_shape
from zlang.ir.functional_regions import FunctionalTable
from zlang.ir.packing import PackingError, packed_width
from zlang.ir.pipelines import PipelinePlan
from zlang.ir.runtime_values import scalar_fits
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.types import (
    BitsType,
    EnumType,
    TaggedUnionType,
    TupleType,
    UIntType,
    VecType,
)
from zlang.opt.ir import CanonicalExpression, ExpressionOp, NodeId
from zlang.opt.lowering_errors import CanonicalizationError


def restore_expression(
    nodes: tuple[CanonicalExpression, ...],
    root: NodeId,
) -> expr.Expression:
    """Restore one typed expression from a self-contained canonical DAG."""

    return _ExpressionRestorer(nodes).restore(root)


class _ExpressionRestorer:
    def __init__(self, nodes: tuple[CanonicalExpression, ...]) -> None:
        self.nodes = nodes
        self.cache: dict[NodeId, expr.Expression] = {}

    def restore(self, node_id: NodeId) -> expr.Expression:
        if isinstance(node_id, bool) or not isinstance(node_id, int) or node_id < 0:
            raise CanonicalizationError(
                f"canonical expression root %{node_id} does not exist"
            )
        if node_id in self.cache:
            return self.cache[node_id]
        try:
            node = self.nodes[node_id]
        except IndexError as error:
            raise CanonicalizationError(
                f"canonical expression root %{node_id} does not exist"
            ) from error
        operands = tuple(self.restore(item) for item in node.operands)
        attribute = node.attribute
        op = node.op
        if op is ExpressionOp.INPUT:
            restored: expr.Expression = expr.InputRef(attribute("name"), node.type)
        elif op is ExpressionOp.PARAMETER:
            restored = expr.ParameterRef(attribute("name"), node.type)
        elif op is ExpressionOp.REGISTER_REF:
            restored = expr.RegisterRef(attribute("name"), node.type)
        elif op is ExpressionOp.READY_VALID_REF:
            restored = expr.ReadyValidRef(
                attribute("interface"), attribute("signal"), node.type
            )
        elif op is ExpressionOp.CREDIT_REF:
            restored = expr.CreditRef(
                attribute("interface"), attribute("signal"), node.type
            )
        elif op is ExpressionOp.PACKET_REF:
            restored = expr.PacketRef(
                attribute("interface"), attribute("signal"), node.type
            )
        elif op is ExpressionOp.VC_CREDIT_REF:
            restored = expr.VirtualChannelCreditRef(
                attribute("interface"), attribute("signal"), node.type
            )
        elif op is ExpressionOp.REQUEST_RESPONSE_REF:
            restored = expr.RequestResponseRef(
                attribute("interface"),
                attribute("channel"),
                attribute("signal"),
                node.type,
            )
        elif op is ExpressionOp.FIFO_REF:
            restored = expr.FifoRef(
                attribute("fifo"), attribute("signal"), node.type
            )
        elif op is ExpressionOp.MEMORY_REF:
            restored = expr.MemoryRef(
                attribute("memory"), attribute("signal"), node.type,
                attribute("port") if "port" in dict(node.attributes) else None,
            )
        elif op is ExpressionOp.ROM_REF:
            restored = expr.RomRef(
                attribute("rom"), attribute("signal"), node.type
            )
        elif op is ExpressionOp.CONSTANT:
            value = attribute("value")
            if not scalar_fits(value, node.type):
                raise CanonicalizationError(
                    f"canonical constant %{node_id} value {value} does not fit "
                    f"exact type {node.type}"
                )
            restored = expr.Constant(value, node.type)
        elif op is ExpressionOp.ENUM_ENCODE:
            restored = expr.EnumEncode(operands[0], node.type)
        elif op is ExpressionOp.ENUM_VALID:
            enum_type = attribute("enum_type")
            if not isinstance(enum_type, EnumType):
                raise CanonicalizationError(
                    f"canonical enum valid %{node_id} has no enum type"
                )
            restored = expr.EnumValid(operands[0], enum_type, node.type)
        elif op is ExpressionOp.ENUM_DECODE:
            if not isinstance(node.type, EnumType):
                raise CanonicalizationError(
                    f"canonical enum decode %{node_id} has non-enum result"
                )
            restored = expr.EnumDecode(operands[0], operands[1], node.type)
        elif op is ExpressionOp.UNION_CONSTRUCT:
            if not isinstance(node.type, TaggedUnionType):
                raise CanonicalizationError(
                    f"canonical union constructor %{node_id} has non-union type"
                )
            try:
                restored = expr.UnionConstruct(
                    attribute("variant"),
                    tuple(zip(attribute("field_names"), operands, strict=True)),
                    node.type,
                )
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical union constructor %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.UNION_TAG:
            try:
                restored = expr.UnionTag(operands[0], node.type)
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical union tag %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.UNION_FIELD:
            try:
                restored = expr.UnionField(
                    operands[0], attribute("variant"), attribute("field"), node.type
                )
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical union field %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.ADD:
            restored = expr.Add(operands[0], operands[1], node.type)
        elif op is ExpressionOp.BINARY:
            restored = expr.Binary(
                attribute("operator"),
                operands[0],
                operands[1],
                attribute("operand_type"),
                node.type,
            )
        elif op is ExpressionOp.EXTEND:
            restored = expr.Extend(operands[0], node.type)
        elif op is ExpressionOp.TRUNCATE:
            restored = expr.Truncate(operands[0], node.type)
        elif op is ExpressionOp.FIXED_CONVERT:
            restored = expr.FixedConvert(
                operands[0], attribute("rounding"), attribute("overflow"),
                attribute("conversion_kind"), node.type,
                attribute("rational_denominator"),
            )
        elif op is ExpressionOp.MUX:
            restored = expr.Mux(operands[0], operands[1], operands[2], node.type)
        elif op is ExpressionOp.SWITCH:
            keys = attribute("keys")
            restored = expr.Switch(
                operands[0],
                tuple(
                    expr.SwitchCase(key, operand)
                    for key, operand in zip(keys, operands[1:-1], strict=True)
                ),
                operands[-1],
                node.type,
            )
        elif op is ExpressionOp.CALL:
            restored = expr.Call(
                attribute("function"),
                operands,
                node.type,
                attribute("callee_identity"),
            )
        elif op is ExpressionOp.FIELD:
            restored = expr.FieldAccess(operands[0], attribute("field"), node.type)
        elif op is ExpressionOp.STRUCT_CONSTRUCT:
            restored = expr.StructConstruct(
                attribute("struct_name"),
                tuple(zip(attribute("field_names"), operands, strict=True)),
                node.type,
            )
        elif op is ExpressionOp.TUPLE_CONSTRUCT:
            if not isinstance(node.type, TupleType):
                raise CanonicalizationError(
                    f"canonical tuple constructor %{node_id} has non-tuple type"
                )
            try:
                restored = expr.TupleConstruct(operands, node.type)
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical tuple constructor %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.TUPLE_PROJECT:
            if len(operands) != 1:
                raise CanonicalizationError(
                    f"canonical tuple projection %{node_id} requires one operand"
                )
            try:
                restored = expr.TupleProject(
                    operands[0], attribute("index"), node.type
                )
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical tuple projection %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.FUNCTIONAL_CAPTURE:
            if operands:
                raise CanonicalizationError(
                    f"canonical functional capture %{node_id} must be a leaf"
                )
            restored = expr.FunctionalCaptureRef(
                attribute("identity"),
                attribute("display_name"),
                node.type,
            )
        elif op is ExpressionOp.FUNCTIONAL_VALUE:
            if operands:
                raise CanonicalizationError(
                    f"canonical functional value %{node_id} must be a leaf"
                )
            try:
                restored = expr.FunctionalValue(attribute("expression"), node.type)
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical functional value %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.FUNCTIONAL_TABLE_LOOKUP:
            if operands:
                raise CanonicalizationError(
                    f"canonical functional lookup %{node_id} must be a leaf"
                )
            try:
                table_name = attribute("table_name")
                index = attribute("index")
                range_minimum = attribute("range_minimum")
                range_maximum = attribute("range_maximum")
                range_provenance = attribute("range_provenance")
            except KeyError as error:
                raise CanonicalizationError(
                    f"canonical functional lookup %{node_id} is missing "
                    f"attribute {error.args[0]!r}"
                ) from error
            present = tuple(
                value is not None
                for value in (range_minimum, range_maximum, range_provenance)
            )
            if any(present) and not all(present):
                raise CanonicalizationError(
                    f"canonical functional lookup %{node_id} has incomplete value range"
                )
            if all(present) and (
                isinstance(range_minimum, bool)
                or not isinstance(range_minimum, int)
                or isinstance(range_maximum, bool)
                or not isinstance(range_maximum, int)
                or not isinstance(range_provenance, str)
                or not range_provenance
            ):
                raise CanonicalizationError(
                    f"canonical functional lookup %{node_id} has invalid value range"
                )
            try:
                restored = expr.FunctionalTableLookup(
                    table_name,
                    index,
                    node.type,
                    (
                        expr.ValueRange(
                            range_minimum,
                            range_maximum,
                            range_provenance,
                        )
                        if all(present) else None
                    ),
                )
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical functional lookup %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.VECTOR_INDEX:
            restored = expr.VectorIndex(operands[0], attribute("index"), node.type)
        elif op is ExpressionOp.RUNTIME_INDEX:
            restored = expr.RuntimeIndex(
                operands[0], operands[1], attribute("vector_length"),
                expr.ValueRange(
                    attribute("range_minimum"), attribute("range_maximum"),
                    attribute("range_provenance"),
                ),
                node.type,
            )
        elif op is ExpressionOp.VECTOR_UPDATE:
            if len(operands) != 3:
                raise CanonicalizationError(
                    f"canonical vector update %{node_id} requires three operands"
                )
            if not isinstance(node.type, VecType) or operands[0].type != node.type:
                raise CanonicalizationError(
                    f"canonical vector update %{node_id} has an invalid vector type"
                )
            if operands[2].type != node.type.element_type:
                raise CanonicalizationError(
                    f"canonical vector update %{node_id} has an invalid element type"
                )
            if not isinstance(operands[1].type, (UIntType, BitsType)):
                raise CanonicalizationError(
                    f"canonical vector update %{node_id} has an invalid index type"
                )
            length = attribute("vector_length")
            minimum = attribute("range_minimum")
            maximum = attribute("range_maximum")
            if length != node.type.length or minimum < 0 or maximum >= length:
                raise CanonicalizationError(
                    f"canonical vector update %{node_id} has invalid range metadata"
                )
            restored = expr.VectorUpdate(
                operands[0], operands[1], operands[2], length,
                expr.ValueRange(
                    minimum, maximum, attribute("range_provenance")
                ),
                node.type,
            )
        elif op is ExpressionOp.SLICE:
            restored = expr.Slice(
                operands[0], attribute("msb"), attribute("lsb"), node.type
            )
        elif op is ExpressionOp.CONCAT:
            if len(operands) < 2:
                raise CanonicalizationError(
                    f"canonical concat %{node_id} requires at least two operands"
                )
            if not isinstance(node.type, BitsType):
                raise CanonicalizationError(
                    f"canonical concat %{node_id} result must be bits<N>"
                )
            widths = attribute("operand_widths")
            if widths != tuple(operand.type.width for operand in operands):
                raise CanonicalizationError(
                    f"canonical concat %{node_id} operand widths do not match"
                )
            if any(isinstance(operand.type, VecType) for operand in operands):
                raise CanonicalizationError(
                    f"canonical concat %{node_id} contains a vector operand"
                )
            try:
                packed_widths = tuple(packed_width(operand.type) for operand in operands)
            except PackingError as error:
                raise CanonicalizationError(
                    f"canonical concat %{node_id} has non-packable operand: {error}"
                ) from error
            if sum(packed_widths) != node.type.width:
                raise CanonicalizationError(
                    f"canonical concat %{node_id} result width does not match operands"
                )
            restored = expr.Concat(operands, node.type)
        elif op is ExpressionOp.BITCAST:
            if len(operands) != 1:
                raise CanonicalizationError(
                    f"canonical bitcast %{node_id} requires exactly one operand"
                )
            if attribute("source_type") != operands[0].type:
                raise CanonicalizationError(
                    f"canonical bitcast %{node_id} source type does not match"
                )
            try:
                if packed_width(operands[0].type) != packed_width(node.type):
                    raise CanonicalizationError(
                        f"canonical bitcast %{node_id} widths do not match"
                    )
            except PackingError as error:
                raise CanonicalizationError(
                    f"canonical bitcast %{node_id} is not bit-packable: {error}"
                ) from error
            restored = expr.Bitcast(operands[0], node.type)
        elif op is ExpressionOp.VECTOR_CONCAT:
            if attribute("operand_types") != tuple(
                operand.type for operand in operands
            ):
                raise CanonicalizationError(
                    f"canonical vector concat %{node_id} operand types do not match"
                )
            if (
                len(operands) < 2
                or not isinstance(node.type, VecType)
                or any(not isinstance(operand.type, VecType) for operand in operands)
            ):
                raise CanonicalizationError(
                    f"canonical vector concat %{node_id} has invalid vector shape"
                )
            vector_operands = tuple(operands)
            if any(
                operand.type.element_type != node.type.element_type
                for operand in vector_operands
                if isinstance(operand.type, VecType)
            ) or sum(
                operand.type.length
                for operand in vector_operands
                if isinstance(operand.type, VecType)
            ) != node.type.length:
                raise CanonicalizationError(
                    f"canonical vector concat %{node_id} result shape does not match"
                )
            restored = expr.VectorConcat(operands, node.type)
        elif op is ExpressionOp.RESHAPE:
            if len(operands) != 1:
                raise CanonicalizationError(
                    f"canonical reshape %{node_id} requires exactly one operand"
                )
            if attribute("source_type") != operands[0].type:
                raise CanonicalizationError(
                    f"canonical reshape %{node_id} source type does not match"
                )
            try:
                source_count, source_leaf = vector_leaf_shape(operands[0].type)
                target_count, target_leaf = vector_leaf_shape(node.type)
            except FunctionalLoweringError as error:
                raise CanonicalizationError(
                    f"canonical reshape %{node_id} has non-vector type: {error}"
                ) from error
            if source_count != target_count or source_leaf != target_leaf:
                raise CanonicalizationError(
                    f"canonical reshape %{node_id} does not preserve leaf sequence"
                )
            restored = expr.Reshape(operands[0], node.type)
        elif op is ExpressionOp.PACK:
            if attribute("source_type") != operands[0].type:
                raise CanonicalizationError(
                    f"canonical pack %{node_id} source type does not match"
                )
            restored = expr.Pack(operands[0], node.type)
        elif op is ExpressionOp.UNPACK:
            if attribute("source_width") != operands[0].type.width:
                raise CanonicalizationError(
                    f"canonical unpack %{node_id} source width does not match"
                )
            restored = expr.Unpack(operands[0], node.type)
        elif op is ExpressionOp.INSTANCE_OUTPUT:
            restored = expr.InstanceOutputRef(
                attribute("instance"),
                attribute("port"),
                node.type,
                domain=attribute("domain"),
            )
        elif op is ExpressionOp.GENERATE:
            restored = expr.Generate(
                attribute("index"),
                attribute("start"),
                attribute("stop"),
                operands,
                node.type,
            )
        elif op is ExpressionOp.FUNCTIONAL_REGION:
            if not operands:
                raise CanonicalizationError(
                    f"canonical functional region %{node_id} requires a template"
                )
            table_layout = attribute("table_layout")
            capture_layout = attribute("capture_layout")
            expected_operands = 1 + sum(
                count for _, _, _, count in table_layout
            ) + len(capture_layout)
            if len(operands) != expected_operands:
                raise CanonicalizationError(
                    f"canonical functional region %{node_id} operand layout does not match"
                )
            cursor = 1
            tables: list[FunctionalTable] = []
            for name, start, type_, count in table_layout:
                values = operands[cursor : cursor + count]
                cursor += count
                tables.append(FunctionalTable(name, values, type_, start))
            captures = tuple(
                (
                    expr.FunctionalCaptureRef(identity, display_name, type_),
                    operands[cursor + offset],
                )
                for offset, (identity, display_name, type_) in enumerate(
                    capture_layout
                )
            )
            try:
                restored = expr.FunctionalRegion(
                    attribute("kind"),
                    attribute("binder"),
                    operands[0],
                    tuple(tables),
                    captures,
                    node.type,
                    attribute("certificates"),
                )
                certificate_template_identity = dict(node.attributes).get(
                    "certificate_template_identity"
                )
                if restored.certificates and certificate_template_identity != (
                    expression_semantic_identity(restored.template)
                ):
                    raise ValueError(
                        "functional specialization certificate template identity "
                        "does not match its region"
                    )
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical functional region %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.MAP:
            restored = expr.Map(
                attribute("index"),
                attribute("start"),
                attribute("stop"),
                operands,
                node.type,
            )
        elif op is ExpressionOp.DOT:
            restored = expr.Dot(
                operands[0],
                operands[1],
                operands[2:],
                node.type,
            )
        elif op is ExpressionOp.REDUCE:
            if len(operands) not in {1, 2}:
                raise CanonicalizationError(
                    f"canonical reduce %{node_id} requires one collection and "
                    "at most one exact expansion"
                )
            expanded = operands[1] if len(operands) == 2 else None
            attributes = dict(node.attributes)
            plan = attributes.get("plan")
            if expanded is not None:
                if attribute("resolution") != "exact_overload":
                    raise CanonicalizationError(
                        f"canonical reduce %{node_id} has invalid aggregate resolution"
                    )
                if attribute("topology") != "balanced_source_order":
                    raise CanonicalizationError(
                        f"canonical reduce %{node_id} has invalid aggregate topology"
                    )
                if expanded.type != node.type:
                    raise CanonicalizationError(
                        f"canonical reduce %{node_id} expansion type does not match"
                    )
            if plan is not None:
                if expanded is not None:
                    raise CanonicalizationError(
                        f"canonical reduce %{node_id} cannot have expansion and plan"
                    )
                if attributes.get("resolution") != "exact_plan":
                    raise CanonicalizationError(
                        f"canonical reduce %{node_id} has invalid plan resolution"
                    )
                if attributes.get("topology") != "balanced_source_order":
                    raise CanonicalizationError(
                        f"canonical reduce %{node_id} has invalid plan topology"
                    )
            try:
                restored = expr.Reduce(
                    attribute("operator"),
                    operands[0],
                    node.type,
                    expanded=expanded,
                    plan=plan,
                )
            except ValueError as error:
                raise CanonicalizationError(
                    f"canonical reduce %{node_id} is invalid: {error}"
                ) from error
        elif op is ExpressionOp.DELAY:
            restored = expr.Delay(
                attribute("cycles"),
                operands[0],
                attribute("instance"),
                node.type,
            )
        elif op is ExpressionOp.PIPELINE:
            pipeline_plan = dict(node.attributes).get("pipeline_plan")
            if pipeline_plan is not None and not isinstance(
                pipeline_plan, PipelinePlan
            ):
                raise CanonicalizationError(
                    f"canonical pipeline %{node_id} has invalid schedule metadata"
                )
            restored = expr.Pipeline(
                attribute("stages"),
                operands[0],
                attribute("instance"),
                node.type,
                pipeline_plan=pipeline_plan,
                domain=dict(node.attributes).get("domain"),
            )
            if pipeline_plan is not None and pipeline_plan.scheduler != "legacy":
                from zlang.pipeline_scheduling import erase_pipeline_timing
                from zlang.timing import timing_info

                physical = replace(restored, pipeline_plan=None)
                if (
                    pipeline_plan.scheduled_expression_identity
                    != expression_semantic_identity(physical)
                ):
                    raise CanonicalizationError(
                        f"canonical pipeline %{node_id} schedule identity disagrees "
                        "with its physical expression"
                    )
                source = erase_pipeline_timing(physical)
                semantic_source = (
                    pipeline_plan.source_expression
                    if pipeline_plan.source_expression is not None
                    else source
                )
                if (
                    pipeline_plan.source_expression_identity
                    != expression_semantic_identity(semantic_source)
                ):
                    raise CanonicalizationError(
                        f"canonical pipeline %{node_id} source identity disagrees "
                        "with its value expression"
                    )
                if (
                    pipeline_plan.selected_value_identity
                    != expression_semantic_identity(source)
                ):
                    raise CanonicalizationError(
                        f"canonical pipeline %{node_id} selected value identity "
                        "disagrees with its physical value expression"
                    )
                if timing_info(restored).latency != pipeline_plan.requested_latency:
                    raise CanonicalizationError(
                        f"canonical pipeline %{node_id} schedule latency disagrees "
                        "with its physical expression"
                    )
        elif op is ExpressionOp.IMPLEMENTATION_CHOICE:
            kinds = attribute("kinds")
            applicability = attribute("applicability")
            semantics = attribute("semantics")
            evidence = attribute("evidence")
            restored = expr.ImplementationChoice(
                attribute("selected"),
                tuple(
                    expr.ImplementationAlternative(
                        kind,
                        operand,
                        applicability_item,
                        semantics_item,
                        evidence_item.estimate,
                        evidence_item.measurement,
                    )
                    for (
                        kind,
                        operand,
                        applicability_item,
                        semantics_item,
                        evidence_item,
                    ) in zip(
                        kinds,
                        operands,
                        applicability,
                        semantics,
                        evidence,
                        strict=True,
                    )
                ),
                attribute("proven_equivalences"),
                node.type,
                attribute("cost_policy"),
            )
        else:
            raise CanonicalizationError(f"unsupported canonical operation {op}")
        if node.origins:
            restored = replace(restored, origin=node.origins[0])
        self.cache[node_id] = restored
        return restored
