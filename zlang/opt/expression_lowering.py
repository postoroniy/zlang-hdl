"""Semantic-expression DAG lowering into canonical optimization IR."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.expression_arena import ExpressionProvenanceTable
from zlang.ir.module import Module
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)
from zlang.opt.ir import (
    CanonicalExpression,
    CanonicalImplementationEvidence,
    EffectKind,
    ExpressionOp,
    NodeCategory,
    NodeId,
    NodeMetadata,
    Purity,
    Signedness,
)
from zlang.opt.lowering_errors import CanonicalizationError


def _signedness(type_: HardwareType) -> Signedness:
    if isinstance(type_, BitType):
        return Signedness.CONTROL
    if isinstance(type_, UIntType):
        return Signedness.UNSIGNED
    if isinstance(type_, SIntType):
        return Signedness.SIGNED
    if isinstance(type_, FixedType):
        return Signedness.SIGNED
    if isinstance(type_, UFixedType):
        return Signedness.UNSIGNED
    if isinstance(type_, (BitsType, EnumType)):
        return Signedness.RAW_BITS
    return Signedness.AGGREGATE


class _ExpressionBuilder:
    def __init__(self, module: Module) -> None:
        self.nodes: list[CanonicalExpression] = []
        self._interned: dict[tuple[object, ...], NodeId] = {}
        self._lowered_objects: dict[tuple[int, str], NodeId] = {}
        self._default_domain = module.clock
        self._port_domains = {port.name: port.domain for port in module.ports}
        self._register_domains = {
            register.name: register.domain or module.clock
            for register in module.registers
        }
        self._memory_latencies = {
            memory.name: memory.read_latency for memory in module.memories
        }
        self._rom_latencies = {
            rom.name: rom.read_latency for rom in module.roms
        }
        provenance = module.semantic_expression_provenance
        self._semantic_provenance = (
            provenance
            if isinstance(provenance, ExpressionProvenanceTable)
            else None
        )

    def _origins(self, expression: expr.Expression) -> tuple[object, ...]:
        if self._semantic_provenance is not None:
            origins = self._semantic_provenance.origins(expression)
            if origins:
                return origins
        return (expression.origin,) if expression.origin is not None else ()

    def lower(self, expression: expr.Expression, scope: str = "module") -> NodeId:
        object_key = (id(expression), scope)
        cached_object = self._lowered_objects.get(object_key)
        if cached_object is not None:
            return cached_object
        category, op, operands, attributes = self._describe(expression, scope)
        metadata = self._metadata(expression, op, operands)
        # A retained call is one compact use-site of a shared callable body.
        # Keep distinct source call sites as distinct canonical nodes so a
        # round trip cannot attribute every invocation to whichever identical
        # call happened to be interned first.  The provenance discriminator is
        # used only for DAG interning; canonical content identity continues to
        # omit origins, and callable definitions remain stored exactly once.
        use_site = expression.origin if isinstance(expression, expr.Call) else None
        key = (
            category,
            op,
            expression.type,
            operands,
            attributes,
            metadata,
            use_site,
        )
        if key in self._interned:
            node_id = self._interned[key]
            merged_origins = tuple(dict.fromkeys((
                *self.nodes[node_id].origins,
                *self._origins(expression),
            )))
            if merged_origins != self.nodes[node_id].origins:
                self.nodes[node_id] = replace(
                    self.nodes[node_id],
                    origins=merged_origins,
                )
            self._lowered_objects[object_key] = node_id
            return node_id
        node_id = len(self.nodes)
        node = CanonicalExpression(
            id=node_id,
            category=category,
            op=op,
            type=expression.type,
            operands=operands,
            attributes=attributes,
            metadata=metadata,
            origins=self._origins(expression),
        )
        self.nodes.append(node)
        self._interned[key] = node_id
        self._lowered_objects[object_key] = node_id
        return node_id

    def _describe(
        self,
        expression: expr.Expression,
        scope: str,
    ) -> tuple[
        NodeCategory,
        ExpressionOp,
        tuple[NodeId, ...],
        tuple[tuple[str, object], ...],
    ]:
        if isinstance(expression, expr.InputRef):
            return self._leaf(NodeCategory.VALUE, ExpressionOp.INPUT, name=expression.name)
        if isinstance(expression, expr.ParameterRef):
            return self._leaf(
                NodeCategory.VALUE,
                ExpressionOp.PARAMETER,
                name=expression.name,
                scope=scope,
            )
        if isinstance(expression, expr.RegisterRef):
            return self._leaf(
                NodeCategory.STATE,
                ExpressionOp.REGISTER_REF,
                name=expression.name,
            )
        if isinstance(expression, expr.ReadyValidRef):
            return self._leaf(
                NodeCategory.PROTOCOL,
                ExpressionOp.READY_VALID_REF,
                interface=expression.interface,
                signal=expression.signal,
            )
        if isinstance(expression, expr.CreditRef):
            return self._leaf(
                NodeCategory.PROTOCOL,
                ExpressionOp.CREDIT_REF,
                interface=expression.interface,
                signal=expression.signal,
            )
        if isinstance(expression, expr.PacketRef):
            return self._leaf(
                NodeCategory.PROTOCOL,
                ExpressionOp.PACKET_REF,
                interface=expression.interface,
                signal=expression.signal,
            )
        if isinstance(expression, expr.VirtualChannelCreditRef):
            return self._leaf(
                NodeCategory.PROTOCOL,
                ExpressionOp.VC_CREDIT_REF,
                interface=expression.interface,
                signal=expression.signal,
            )
        if isinstance(expression, expr.RequestResponseRef):
            return self._leaf(
                NodeCategory.TRANSACTION,
                ExpressionOp.REQUEST_RESPONSE_REF,
                interface=expression.interface,
                channel=expression.channel,
                signal=expression.signal,
            )
        if isinstance(expression, expr.FifoRef):
            return self._leaf(
                NodeCategory.STATE,
                ExpressionOp.FIFO_REF,
                fifo=expression.fifo,
                signal=expression.signal,
            )
        if isinstance(expression, expr.MemoryRef):
            return self._leaf(
                NodeCategory.STATE,
                ExpressionOp.MEMORY_REF,
                memory=expression.memory,
                signal=expression.signal,
                port=expression.port,
            )
        if isinstance(expression, expr.RomRef):
            return self._leaf(
                NodeCategory.STATE,
                ExpressionOp.ROM_REF,
                rom=expression.rom,
                signal=expression.signal,
            )
        if isinstance(expression, expr.Constant):
            return self._leaf(
                NodeCategory.VALUE,
                ExpressionOp.CONSTANT,
                value=expression.value,
            )
        if isinstance(expression, expr.EnumEncode):
            return self._compound(
                ExpressionOp.ENUM_ENCODE, (expression.expression,), scope
            )
        if isinstance(expression, expr.EnumValid):
            return self._compound(
                ExpressionOp.ENUM_VALID,
                (expression.expression,),
                scope,
                enum_type=expression.enum_type,
            )
        if isinstance(expression, expr.EnumDecode):
            return self._compound(
                ExpressionOp.ENUM_DECODE,
                (expression.expression, expression.fallback),
                scope,
            )
        if isinstance(expression, expr.UnionConstruct):
            return self._compound(
                ExpressionOp.UNION_CONSTRUCT,
                tuple(value for _, value in expression.fields),
                scope,
                variant=expression.variant,
                field_names=tuple(name for name, _ in expression.fields),
            )
        if isinstance(expression, expr.UnionTag):
            return self._compound(
                ExpressionOp.UNION_TAG, (expression.expression,), scope
            )
        if isinstance(expression, expr.UnionField):
            return self._compound(
                ExpressionOp.UNION_FIELD,
                (expression.expression,),
                scope,
                variant=expression.variant,
                field=expression.field,
            )
        if isinstance(expression, expr.Add):
            return self._compound(
                ExpressionOp.ADD,
                (expression.left, expression.right),
                scope,
            )
        if isinstance(expression, expr.Binary):
            return self._compound(
                ExpressionOp.BINARY,
                (expression.left, expression.right),
                scope,
                operator=expression.operator,
                operand_type=expression.operand_type,
            )
        if isinstance(expression, expr.Extend):
            return self._compound(ExpressionOp.EXTEND, (expression.expression,), scope)
        if isinstance(expression, expr.Truncate):
            return self._compound(ExpressionOp.TRUNCATE, (expression.expression,), scope)
        if isinstance(expression, expr.FixedConvert):
            return self._compound(
                ExpressionOp.FIXED_CONVERT, (expression.expression,), scope,
                rounding=expression.rounding, overflow=expression.overflow,
                conversion_kind=expression.kind,
                rational_denominator=expression.rational_denominator,
            )
        if isinstance(expression, expr.Mux):
            return self._compound(
                ExpressionOp.MUX,
                (
                    expression.condition,
                    expression.when_true,
                    expression.when_false,
                ),
                scope,
            )
        if isinstance(expression, expr.Switch):
            return self._compound(
                ExpressionOp.SWITCH,
                (
                    expression.selector,
                    *(case.expression for case in expression.cases),
                    expression.default,
                ),
                scope,
                keys=tuple(case.key for case in expression.cases),
            )
        if isinstance(expression, expr.Call):
            return self._compound(
                ExpressionOp.CALL,
                expression.arguments,
                scope,
                function=expression.function,
                callee_identity=expression.callee_identity,
            )
        if isinstance(expression, expr.FieldAccess):
            return self._compound(
                ExpressionOp.FIELD,
                (expression.expression,),
                scope,
                field=expression.field,
            )
        if isinstance(expression, expr.StructConstruct):
            return self._compound(
                ExpressionOp.STRUCT_CONSTRUCT,
                tuple(value for _, value in expression.fields),
                scope,
                struct_name=expression.struct_name,
                field_names=tuple(name for name, _ in expression.fields),
            )
        if isinstance(expression, expr.TupleConstruct):
            return self._compound(
                ExpressionOp.TUPLE_CONSTRUCT,
                expression.elements,
                scope,
            )
        if isinstance(expression, expr.TupleProject):
            return self._compound(
                ExpressionOp.TUPLE_PROJECT,
                (expression.expression,),
                scope,
                index=expression.index,
            )
        if isinstance(expression, expr.FunctionalCaptureRef):
            return self._leaf(
                NodeCategory.VALUE,
                ExpressionOp.FUNCTIONAL_CAPTURE,
                identity=expression.identity,
                display_name=expression.display_name,
            )
        if isinstance(expression, expr.FunctionalValue):
            return self._leaf(
                NodeCategory.VALUE,
                ExpressionOp.FUNCTIONAL_VALUE,
                expression=expression.expression,
            )
        if isinstance(expression, expr.FunctionalTableLookup):
            value_range = expression.value_range
            return self._leaf(
                NodeCategory.VALUE,
                ExpressionOp.FUNCTIONAL_TABLE_LOOKUP,
                table_name=expression.table_name,
                index=expression.index,
                range_minimum=(
                    value_range.minimum if value_range is not None else None
                ),
                range_maximum=(
                    value_range.maximum if value_range is not None else None
                ),
                range_provenance=(
                    value_range.provenance if value_range is not None else None
                ),
            )
        if isinstance(expression, expr.VectorIndex):
            return self._compound(
                ExpressionOp.VECTOR_INDEX,
                (expression.expression,),
                scope,
                index=expression.index,
            )
        if isinstance(expression, expr.RuntimeIndex):
            return self._compound(
                ExpressionOp.RUNTIME_INDEX,
                (expression.expression, expression.index),
                scope,
                vector_length=expression.vector_length,
                range_minimum=expression.index_range.minimum,
                range_maximum=expression.index_range.maximum,
                range_provenance=expression.index_range.provenance,
            )
        if isinstance(expression, expr.VectorUpdate):
            return self._compound(
                ExpressionOp.VECTOR_UPDATE,
                (expression.expression, expression.index, expression.value),
                scope,
                vector_length=expression.vector_length,
                range_minimum=expression.index_range.minimum,
                range_maximum=expression.index_range.maximum,
                range_provenance=expression.index_range.provenance,
            )
        if isinstance(expression, expr.Slice):
            return self._compound(
                ExpressionOp.SLICE,
                (expression.expression,),
                scope,
                msb=expression.msb,
                lsb=expression.lsb,
            )
        if isinstance(expression, expr.Concat):
            return self._compound(
                ExpressionOp.CONCAT,
                expression.operands,
                scope,
                operand_widths=tuple(
                    operand.type.width for operand in expression.operands
                ),
            )
        if isinstance(expression, expr.Bitcast):
            return self._compound(
                ExpressionOp.BITCAST,
                (expression.expression,),
                scope,
                source_type=expression.expression.type,
            )
        if isinstance(expression, expr.VectorConcat):
            return self._compound(
                ExpressionOp.VECTOR_CONCAT,
                expression.operands,
                scope,
                operand_types=tuple(
                    operand.type for operand in expression.operands
                ),
            )
        if isinstance(expression, expr.Reshape):
            return self._compound(
                ExpressionOp.RESHAPE,
                (expression.expression,),
                scope,
                source_type=expression.expression.type,
            )
        if isinstance(expression, expr.Pack):
            return self._compound(
                ExpressionOp.PACK,
                (expression.expression,),
                scope,
                source_type=expression.expression.type,
            )
        if isinstance(expression, expr.Unpack):
            return self._compound(
                ExpressionOp.UNPACK,
                (expression.expression,),
                scope,
                source_width=expression.expression.type.width,
            )
        if isinstance(expression, expr.InstanceOutputRef):
            return self._leaf(
                NodeCategory.VALUE,
                ExpressionOp.INSTANCE_OUTPUT,
                instance=expression.instance,
                port=expression.port,
                domain=expression.domain,
            )
        if isinstance(expression, expr.Generate):
            return self._compound(
                ExpressionOp.GENERATE,
                expression.elements,
                scope,
                index=expression.index,
                start=expression.start,
                stop=expression.stop,
            )
        if isinstance(expression, expr.FunctionalRegion):
            table_layout = tuple(
                (table.name, table.start, table.type, len(table.values))
                for table in expression.tables
            )
            capture_layout = tuple(
                (reference.identity, reference.display_name, reference.type)
                for reference, _ in expression.captures
            )
            return self._compound(
                ExpressionOp.FUNCTIONAL_REGION,
                (
                    expression.template,
                    *(value for table in expression.tables for value in table.values),
                    *(value for _, value in expression.captures),
                ),
                scope,
                kind=expression.kind,
                binder=expression.binder,
                table_layout=table_layout,
                capture_layout=capture_layout,
                certificates=expression.certificates,
                certificate_template_identity=(
                    expression_semantic_identity(expression.template)
                    if expression.certificates
                    else None
                ),
            )
        if isinstance(expression, expr.Map):
            return self._compound(
                ExpressionOp.MAP,
                expression.elements,
                scope,
                index=expression.index,
                start=expression.start,
                stop=expression.stop,
            )
        if isinstance(expression, expr.Dot):
            return self._compound(
                ExpressionOp.DOT,
                (expression.left, expression.right, *expression.products),
                scope,
            )
        if isinstance(expression, expr.Reduce):
            operands = (
                (expression.collection,)
                if expression.expanded is None
                else (expression.collection, expression.expanded)
            )
            attributes: dict[str, object] = {
                "operator": expression.operator,
            }
            if expression.expanded is not None:
                attributes.update(
                    resolution="exact_overload",
                    topology="balanced_source_order",
                )
            elif expression.plan is not None:
                attributes.update(
                    plan=expression.plan,
                    resolution="exact_plan",
                    topology=expression.plan.ordering,
                )
            return self._compound(
                ExpressionOp.REDUCE,
                operands,
                scope,
                **attributes,
            )
        if isinstance(expression, expr.Delay):
            return self._compound(
                ExpressionOp.DELAY,
                (expression.expression,),
                scope,
                category=NodeCategory.STATE,
                cycles=expression.cycles,
                instance=expression.instance,
            )
        if isinstance(expression, expr.Pipeline):
            attributes: dict[str, object] = {
                "stages": expression.stages,
                "instance": expression.instance,
                "domain": expression.domain,
            }
            if expression.pipeline_plan is not None:
                attributes["pipeline_plan"] = expression.pipeline_plan
            return self._compound(
                ExpressionOp.PIPELINE,
                (expression.expression,),
                scope,
                category=NodeCategory.STATE,
                **attributes,
            )
        if isinstance(expression, expr.ImplementationChoice):
            return self._compound(
                ExpressionOp.IMPLEMENTATION_CHOICE,
                tuple(
                    alternative.expression
                    for alternative in expression.alternatives
                ),
                scope,
                category=NodeCategory.ARCHITECTURE,
                selected=expression.selected,
                kinds=tuple(
                    alternative.kind for alternative in expression.alternatives
                ),
                applicability=tuple(
                    alternative.applicability
                    for alternative in expression.alternatives
                ),
                semantics=tuple(
                    alternative.semantics
                    for alternative in expression.alternatives
                ),
                evidence=tuple(
                    CanonicalImplementationEvidence(
                        alternative.kind,
                        alternative.estimate,
                        alternative.measurement,
                    )
                    for alternative in expression.alternatives
                ),
                proven_equivalences=expression.proven_equivalences,
                cost_policy=expression.cost_policy,
            )
        raise CanonicalizationError(f"unsupported semantic expression {expression!r}")

    def _metadata(
        self,
        expression: expr.Expression,
        op: ExpressionOp,
        operands: tuple[NodeId, ...],
    ) -> NodeMetadata:
        operand_metadata = tuple(
            self.nodes[operand].metadata for operand in operands
        )
        latency = max((item.latency for item in operand_metadata), default=0)
        initiation_interval = max(
            (item.initiation_interval for item in operand_metadata),
            default=1,
        )
        domains = {
            domain for item in operand_metadata for domain in item.domains
        }
        effects = {
            effect for item in operand_metadata for effect in item.effects
        }

        if op is ExpressionOp.INPUT:
            domain = self._port_domains.get(expression.name)
            if domain is not None:
                domains.add(domain)
        elif op is ExpressionOp.REGISTER_REF:
            domain = self._register_domains.get(expression.name)
            if domain is not None:
                domains.add(domain)
            effects.add(EffectKind.READ_STATE)
        elif op in {
            ExpressionOp.READY_VALID_REF,
            ExpressionOp.CREDIT_REF,
            ExpressionOp.PACKET_REF,
            ExpressionOp.VC_CREDIT_REF,
        }:
            domain = self._port_domains.get(expression.interface)
            if domain is not None:
                domains.add(domain)
            effects.add(EffectKind.OBSERVE_PROTOCOL)
        elif op is ExpressionOp.REQUEST_RESPONSE_REF:
            if self._default_domain is not None:
                domains.add(self._default_domain)
            effects.add(EffectKind.OBSERVE_TRANSACTION)
        elif op is ExpressionOp.INSTANCE_OUTPUT:
            if expression.domain is not None:
                domains.add(expression.domain)
        elif op is ExpressionOp.FIFO_REF:
            if self._default_domain is not None:
                domains.add(self._default_domain)
            effects.add(EffectKind.READ_STATE)
        elif op is ExpressionOp.MEMORY_REF:
            if self._default_domain is not None:
                domains.add(self._default_domain)
            latency = max(latency, self._memory_latencies.get(expression.memory, 0))
            effects.add(EffectKind.READ_STATE)
        elif op is ExpressionOp.ROM_REF:
            if self._default_domain is not None:
                domains.add(self._default_domain)
            latency = max(latency, self._rom_latencies.get(expression.rom, 0))
            effects.add(EffectKind.READ_STATE)
        elif op is ExpressionOp.DELAY:
            latency += expression.cycles
            if not domains and self._default_domain is not None:
                domains.add(self._default_domain)
            effects.add(EffectKind.TIME_SHIFT)
        elif op is ExpressionOp.PIPELINE:
            latency += expression.stages
            pipeline_domain = expression.domain or self._default_domain
            if pipeline_domain is not None:
                domains.add(pipeline_domain)
            effects.add(EffectKind.TIME_SHIFT)
        elif op is ExpressionOp.IMPLEMENTATION_CHOICE:
            semantics = expression.alternatives[0].semantics
            latency = max(latency, semantics.latency)
            initiation_interval = max(
                initiation_interval,
                semantics.initiation_interval,
            )
            effects.add(EffectKind.SELECT_ARCHITECTURE)

        ordered_effects = tuple(
            effect for effect in EffectKind if effect in effects
        )
        purity = (
            Purity.ARCHITECTURAL
            if EffectKind.SELECT_ARCHITECTURE in effects
            else Purity.OBSERVATIONAL
            if effects
            else Purity.PURE
        )
        return NodeMetadata(
            width=expression.type.width,
            signedness=_signedness(expression.type),
            latency=latency,
            initiation_interval=initiation_interval,
            domains=tuple(sorted(domains)),
            purity=purity,
            effects=ordered_effects,
        )

    @staticmethod
    def _leaf(
        category: NodeCategory,
        op: ExpressionOp,
        **attributes: object,
    ) -> tuple[
        NodeCategory,
        ExpressionOp,
        tuple[NodeId, ...],
        tuple[tuple[str, object], ...],
    ]:
        return category, op, (), tuple(attributes.items())

    def _compound(
        self,
        op: ExpressionOp,
        operands: tuple[expr.Expression, ...],
        scope: str,
        *,
        category: NodeCategory = NodeCategory.VALUE,
        **attributes: object,
    ) -> tuple[
        NodeCategory,
        ExpressionOp,
        tuple[NodeId, ...],
        tuple[tuple[str, object], ...],
    ]:
        return (
            category,
            op,
            tuple(self.lower(operand, scope) for operand in operands),
            tuple(attributes.items()),
        )


def lower_expression_graph(
    module: Module,
    expression: expr.Expression,
    *,
    scope: str = "module",
) -> tuple[tuple[CanonicalExpression, ...], NodeId]:
    """Lower one semantic expression with the module's typed environment.

    Consumers that deliberately transform one semantic root, such as bounded
    callable expansion before e-graph optimization, can reuse the canonical expression builder
    without manufacturing a synthetic module or depending on its private
    implementation class.
    """

    builder = _ExpressionBuilder(module)
    root = builder.lower(expression, scope)
    return tuple(builder.nodes), root
