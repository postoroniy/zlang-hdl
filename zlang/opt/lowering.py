"""Lossless conversion between semantic IR and canonical optimization IR."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import (
    Assignment,
    Function,
    Module,
    NextAssignment,
    Port,
    PortDirection,
    Register,
    RequestResponseInterface,
    Rule,
)
from zlang.ir.storage import (
    Fifo,
    Memory,
    MemoryResetPolicy,
    Rom,
    memory_byte_mask_width,
)
from zlang.ir.external import ExternalModuleContract
from zlang.ir.state import (
    ActionGroup,
    ResolvedTransition,
    StateAction,
    StateActionKind,
    StateResourceKind,
    actions_conflict,
    ordered_groups,
)
from zlang.ir.verification import (
    Contract,
    VerificationGoal,
    VerificationRequirement,
    VerificationScope,
)
from zlang.ir.pipelines import PipelineCandidate, PipelineExploration, PipelinePlan
from zlang.ir.elastic import ElasticPipelineRegion
from zlang.ir.architectures import (
    ArchitectureCandidate,
    ArchitectureExploration,
)
from zlang.opt.ir import (
    CanonicalArchitectureCandidate,
    CanonicalArchitectureExploration,
    CanonicalAssignment,
    CanonicalContract,
    CanonicalVerificationGoal,
    CanonicalVerificationRequirement,
    CanonicalVerificationScope,
    CanonicalEntity,
    CanonicalExpression,
    CanonicalFifo,
    CanonicalFunction,
    CanonicalExternalModuleContract,
    CanonicalImplementationEvidence,
    CanonicalMemory,
    CanonicalRom,
    CanonicalModule,
    CanonicalNextAssignment,
    CanonicalPipelineCandidate,
    CanonicalPipelineExploration,
    CanonicalElasticPipelineRegion,
    CanonicalRegister,
    CanonicalRule,
    CanonicalActionGroup,
    CanonicalResolvedTransition,
    CanonicalStateAction,
    ExpressionOp,
    EffectKind,
    NodeCategory,
    NodeMetadata,
    NodeId,
    OptimizationStage,
    Purity,
    Signedness,
    TargetKind,
)
from zlang.ir.functional import FunctionalLoweringError, vector_leaf_shape
from zlang.ir.functional_regions import FunctionalTable
from zlang.ir.hierarchy import (
    HierarchyError,
    validate_hierarchical_connections,
    validate_instance_port_bindings,
)
from zlang.ir.packing import PackingError, packed_width
from zlang.ir.runtime_values import scalar_fits
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
    VecType,
    TaggedUnionType,
    TupleType,
)


class CanonicalizationError(ValueError):
    """Semantic IR cannot be represented or restored losslessly."""


class _ExpressionBuilder:
    def __init__(self, module: Module) -> None:
        self.nodes: list[CanonicalExpression] = []
        self._interned: dict[tuple[object, ...], NodeId] = {}
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

    def lower(self, expression: expr.Expression, scope: str = "module") -> NodeId:
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
            if (
                expression.origin is not None
                and expression.origin not in self.nodes[node_id].origins
            ):
                self.nodes[node_id] = replace(
                    self.nodes[node_id],
                    origins=(*self.nodes[node_id].origins, expression.origin),
                )
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
            origins=(expression.origin,) if expression.origin is not None else (),
        )
        self.nodes.append(node)
        self._interned[key] = node_id
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
            if not domains and self._default_domain is not None:
                domains.add(self._default_domain)
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
    callable expansion before M26, can reuse the canonical expression builder
    without manufacturing a synthetic module or depending on its private
    implementation class.
    """

    builder = _ExpressionBuilder(module)
    root = builder.lower(expression, scope)
    return tuple(builder.nodes), root


def lower(
    module: Module,
    *,
    stage: OptimizationStage = OptimizationStage.HIGH_LEVEL,
) -> CanonicalModule:
    """Normalize semantic IR into a deterministic canonical DAG."""

    builder = _ExpressionBuilder(module)
    functions = tuple(
        CanonicalFunction(
            function.name,
            function.parameters,
            function.return_type,
            builder.lower(function.body, f"function:{function.name}"),
            function.callee_identity,
            function.metadata,
        )
        for function in module.functions
    )
    callable_definitions = tuple(
        CanonicalFunction(
            function.name,
            function.parameters,
            function.return_type,
            builder.lower(
                function.body,
                f"callable:{function.callee_identity}",
            ),
            function.callee_identity,
            function.metadata,
        )
        for function in sorted(
            module.callable_definitions,
            key=lambda item: item.callee_identity,
        )
    )
    assignments = tuple(
        CanonicalAssignment(
            _target_kind(assignment.target),
            assignment.target.name,
            builder.lower(assignment.expression),
            assignment.signal,
            assignment.channel,
        )
        for assignment in module.assignments
    )
    registers = tuple(
        CanonicalRegister(
            register.name,
            register.type,
            builder.lower(register.initial),
            register.domain,
        )
        for register in module.registers
    )
    next_assignments = tuple(
        CanonicalNextAssignment(
            _target_kind(assignment.target),
            assignment.target.name,
            builder.lower(assignment.expression),
            (
                builder.lower(assignment.activation)
                if assignment.activation is not None else None
            ),
        )
        for assignment in module.next_assignments
    )
    rules = tuple(
        CanonicalRule(
            rule.name,
            builder.lower(rule.guard),
            tuple(
                CanonicalNextAssignment(
                    _target_kind(action.target),
                    action.target.name,
                    builder.lower(action.expression),
                    (
                        builder.lower(action.activation)
                        if action.activation is not None else None
                    ),
                )
                for action in rule.actions
            ),
        )
        for rule in module.rules
    )
    fifos = tuple(
        CanonicalFifo(
            fifo.name,
            fifo.element_type,
            fifo.depth,
            builder.lower(fifo.data) if fifo.data is not None else None,
            builder.lower(fifo.push) if fifo.push is not None else None,
            builder.lower(fifo.pop) if fifo.pop is not None else None,
            fifo.source_origin,
        )
        for fifo in module.fifos
    )
    resolved_transition = (
        CanonicalResolvedTransition(
            module.resolved_transition.semantic_id,
            module.resolved_transition.domain,
            module.resolved_transition.reset,
            module.resolved_transition.resources,
            tuple(
                CanonicalActionGroup(
                    group.semantic_id, group.rule_name,
                    builder.lower(group.guard, f"action-group:{group.rule_name}"),
                    tuple(
                        CanonicalStateAction(
                            action.semantic_id, action.resource_id, action.kind,
                            tuple(builder.lower(operand, f"action:{action.semantic_id}") for operand in action.operands),
                            action.owner_group, action.source_origin,
                            (
                                builder.lower(
                                    action.activation,
                                    f"action-activation:{action.semantic_id}",
                                )
                                if action.activation is not None else None
                            ),
                        ) for action in group.actions
                    ),
                    group.source_origin,
                ) for group in module.resolved_transition.action_groups
            ),
            module.resolved_transition.priorities,
        )
        if module.resolved_transition is not None else None
    )
    memories = tuple(
        CanonicalMemory(
            memory.name,
            memory.semantic_id,
            memory.element_type,
            memory.depth,
            memory.read_latency,
            memory.collision,
            builder.lower(memory.read_address) if memory.read_address is not None else None,
            builder.lower(memory.write_enable) if memory.write_enable is not None else None,
            builder.lower(memory.write_address) if memory.write_address is not None else None,
            builder.lower(memory.write_data) if memory.write_data is not None else None,
            memory.source_origin,
            write_mask_width=memory.write_mask_width,
            write_mask=(
                builder.lower(memory.write_mask)
                if memory.write_mask is not None else None
            ),
            contents_reset=memory.contents_reset,
            read_data_reset=memory.read_data_reset,
        )
        for memory in module.memories
    )
    roms = tuple(
        CanonicalRom(
            rom.name,
            rom.semantic_id,
            rom.element_type,
            rom.depth,
            rom.address_type,
            rom.read_latency,
            tuple(builder.lower(word, f"rom:{rom.semantic_id}:contents") for word in rom.contents),
            builder.lower(rom.read_address, f"rom:{rom.semantic_id}:address"),
            rom.initialization_identity,
            rom.dependency_identity,
            rom.evaluator_schema,
            rom.content_hash,
            rom.source_origin,
        )
        for rom in module.roms
    )
    contracts = tuple(
        CanonicalContract(
            contract.kind,
            contract.name,
            contract.clock,
            contract.reset,
            builder.lower(contract.expression),
        )
        for contract in module.contracts
    )
    verification_builder = _ExpressionBuilder(module)
    verification_scopes = tuple(
        CanonicalVerificationScope(
            scope.semantic_id,
            scope.name,
            scope.clock,
            scope.reset,
            tuple(
                CanonicalVerificationRequirement(
                    requirement.semantic_id,
                    requirement.name,
                    verification_builder.lower(
                        requirement.expression,
                        f"verification-requirement:{requirement.semantic_id}",
                    ),
                    requirement.source_origin,
                )
                for requirement in scope.requirements
            ),
            tuple(
                CanonicalVerificationGoal(
                    goal.semantic_id,
                    goal.scope_id,
                    goal.kind,
                    goal.name,
                    verification_builder.lower(
                        goal.expression,
                        f"verification-goal:{goal.semantic_id}",
                    ),
                    goal.source_origin,
                )
                for goal in scope.goals
            ),
            scope.source_origin,
        )
        for scope in module.verification_scopes
    )
    pipeline_explorations = tuple(
        CanonicalPipelineExploration(
            exploration.output,
            exploration.result_type,
            builder.lower(exploration.source_expression),
            exploration.constraints,
            tuple(
                CanonicalPipelineCandidate(
                    candidate.name,
                    builder.lower(candidate.expression),
                    candidate.tree,
                    candidate.register_placement,
                    candidate.multiplier_mapping,
                    candidate.transformations,
                    candidate.latency,
                    candidate.initiation_interval,
                    candidate.estimate,
                    candidate.cost_source,
                    candidate.violations,
                    candidate.pipeline_plan,
                )
                for candidate in exploration.candidates
            ),
            exploration.selected,
            exploration.search_bound,
        )
        for exploration in module.pipeline_explorations
    )
    elastic_pipeline_regions = tuple(
        CanonicalElasticPipelineRegion(
            region.semantic_id,
            region.source_endpoint,
            region.destination_endpoint,
            region.input_type,
            region.output_type,
            builder.lower(region.source_expression),
            region.constraints,
            tuple(
                CanonicalPipelineCandidate(
                    candidate.name,
                    builder.lower(candidate.expression),
                    candidate.tree,
                    candidate.register_placement,
                    candidate.multiplier_mapping,
                    candidate.transformations,
                    candidate.latency,
                    candidate.initiation_interval,
                    candidate.estimate,
                    candidate.cost_source,
                    candidate.violations,
                    candidate.pipeline_plan,
                )
                for candidate in region.candidates
            ),
            region.selected,
            region.plan,
            region.timing,
            region.clock,
            region.reset,
            region.source_origin,
        )
        for region in module.elastic_pipeline_regions
    )
    architecture_explorations = tuple(
        CanonicalArchitectureExploration(
            exploration.output,
            exploration.result_type,
            builder.lower(exploration.source_expression),
            exploration.constraints,
            tuple(
                CanonicalArchitectureCandidate(
                    candidate.name,
                    builder.lower(candidate.expression),
                    candidate.kind,
                    candidate.parallelism,
                    candidate.add_depth,
                    candidate.multiplier_count,
                    candidate.adder_count,
                    candidate.transformations,
                    candidate.equivalence,
                    candidate.violations,
                )
                for candidate in exploration.candidates
            ),
            exploration.selected,
            exploration.theoretical_candidates,
            exploration.search_bound,
            exploration.budget_pruned,
            exploration.constraint_pruned,
        )
        for exploration in module.architecture_explorations
    )
    entities = _build_entities(
        module,
        functions,
        callable_definitions,
        assignments,
        registers,
        next_assignments,
        rules,
        fifos,
        memories,
        roms,
        contracts,
        pipeline_explorations,
        architecture_explorations,
    )
    return CanonicalModule(
        name=module.name,
        stage=stage,
        expressions=tuple(builder.nodes),
        entities=entities,
        ports=module.ports,
        assignments=assignments,
        structs=module.structs,
        enums=module.enums,
        tagged_unions=module.tagged_unions,
        functions=functions,
        callable_definitions=callable_definitions,
        clock=module.clock,
        reset=module.reset,
        registers=registers,
        next_assignments=next_assignments,
        request_responses=module.request_responses,
        connections=module.connections,
        csr_blocks=module.csr_blocks,
        csr_access=module.csr_access,
        rules=rules,
        rule_priorities=module.rule_priorities,
        fifos=fifos,
        memories=memories,
        roms=roms,
        clock_domains=module.clock_domains,
        arbiters=module.arbiters,
        contracts=contracts,
        pipeline_explorations=pipeline_explorations,
        architecture_explorations=architecture_explorations,
        equivalences=module.equivalences,
        locals=module.locals,
        instances=module.instances,
        parameters=module.parameters,
        instance_bindings=module.instance_bindings,
        children=module.children,
        elaborated_instances=module.elaborated_instances,
        protocol_endpoints=module.protocol_endpoints,
        hierarchical_connections=module.hierarchical_connections,
        request_response_connections=module.request_response_connections,
        protocol_schemas=module.protocol_schemas,
        aggregate_protocol_endpoints=module.aggregate_protocol_endpoints,
        aggregate_protocol_connections=module.aggregate_protocol_connections,
        library_imports=module.library_imports,
        library_dependencies=module.library_dependencies,
        generic_specializations=module.generic_specializations,
        resolved_transition=resolved_transition,
        timing_contract=module.timing_contract,
        output_timings=module.output_timings,
        instance_output_timings=module.instance_output_timings,
        root_module_identity=module.root_module_identity,
        dependency_closure=module.dependency_closure,
        module_signature=module.module_signature,
        external_contract=(
            CanonicalExternalModuleContract(
                module.external_contract.logical_name,
                module.external_contract.signature,
                module.external_contract.model_callee_identity,
                module.external_contract.semantic_identity,
                module.external_contract.source_origin,
            )
            if module.external_contract is not None else None
        ),
        elastic_pipeline_regions=elastic_pipeline_regions,
        specialization_bindings=module.specialization_bindings,
        verification_scopes=verification_scopes,
        verification_expressions=tuple(verification_builder.nodes),
    )


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


def restore(module: CanonicalModule) -> Module:
    """Restore semantic IR exactly from canonical metadata and expression roots."""

    for domain in module.clock_domains:
        try:
            domain.validate()
        except ValueError as error:
            raise CanonicalizationError(
                "canonical physical clock/reset contract is invalid: "
                f"{error}"
            ) from error
    if len(module.clock_domains) == 1 and (
        module.clock != module.clock_domains[0].clock
        or module.reset != module.clock_domains[0].reset
    ):
        raise CanonicalizationError(
            "canonical module clock/reset names disagree with its physical domain"
        )
    if (
        module.module_signature is not None
        and module.module_signature.clock_domains != module.clock_domains
    ):
        raise CanonicalizationError(
            "canonical module signature physical clock/reset contract disagrees "
            "with the module"
        )

    expressions = _ExpressionRestorer(module.expressions)
    verification_expressions = _ExpressionRestorer(
        module.verification_expressions
    )
    verification_domains = {
        domain.clock: domain for domain in module.clock_domains
    }
    for scope in module.verification_scopes:
        domain = verification_domains.get(scope.clock)
        if domain is None:
            raise CanonicalizationError(
                f"canonical verification scope '{scope.name}' references "
                f"missing clock '{scope.clock}'"
            )
        if scope.reset != domain.reset:
            raise CanonicalizationError(
                f"canonical verification scope '{scope.name}' must use reset "
                f"'{domain.reset}' for clock '{scope.clock}'"
            )
    ports = {port.name: port for port in module.ports}
    request_responses = {
        interface.name: interface for interface in module.request_responses
    }
    registers = tuple(
        Register(
            register.name,
            register.type,
            expressions.restore(register.initial),
            register.domain,
        )
        for register in module.registers
    )
    register_symbols = {register.name: register for register in registers}

    def target(kind: TargetKind, name: str) -> Port | RequestResponseInterface | Register:
        if kind is TargetKind.PORT:
            try:
                return ports[name]
            except KeyError as error:
                raise CanonicalizationError(
                    f"canonical target references missing port '{name}'"
                ) from error
        if kind is TargetKind.REQUEST_RESPONSE:
            try:
                return request_responses[name]
            except KeyError as error:
                raise CanonicalizationError(
                    f"canonical target references missing interface '{name}'"
                ) from error
        try:
            return register_symbols[name]
        except KeyError as error:
            raise CanonicalizationError(
                f"canonical target references missing register '{name}'"
            ) from error

    assignments = tuple(
        Assignment(
            target(item.target_kind, item.target_name),
            expressions.restore(item.expression),
            item.signal,
            item.channel,
        )
        for item in module.assignments
    )
    def restore_activation(
        node: NodeId | None,
        label: str,
    ) -> expr.Expression | None:
        if node is None:
            return None
        activation = expressions.restore(node)
        if activation.type != BitType():
            raise CanonicalizationError(
                f"canonical {label} activation must have type bit"
            )
        return activation

    def restore_guard(node: NodeId, label: str) -> expr.Expression:
        guard = expressions.restore(node)
        if guard.type != BitType():
            raise CanonicalizationError(
                f"canonical {label} guard must have type bit"
            )
        return guard

    next_assignments = tuple(
        NextAssignment(
            target(item.target_kind, item.target_name),
            expressions.restore(item.expression),
            restore_activation(item.activation, "next-state assignment"),
        )
        for item in module.next_assignments
    )
    if any(item.activation is not None for item in next_assignments):
        raise CanonicalizationError(
            "canonical module-level next-state assignment cannot be conditional"
        )
    rules = tuple(
        Rule(
            rule.name,
            restore_guard(rule.guard, f"rule '{rule.name}'"),
            tuple(
                NextAssignment(
                    target(action.target_kind, action.target_name),
                    expressions.restore(action.expression),
                    restore_activation(
                        action.activation,
                        f"rule '{rule.name}' action",
                    ),
                )
                for action in rule.actions
            ),
        )
        for rule in module.rules
    )
    for memory in module.memories:
        if not memory.semantic_id:
            raise CanonicalizationError(
                "canonical memory semantic identity must not be empty"
            )
        controls = (
            memory.read_address, memory.write_enable,
            memory.write_address, memory.write_data,
        )
        if any(item is None for item in controls) and not all(
            item is None for item in controls
        ):
            raise CanonicalizationError(
                "canonical memory controls must be all present or all absent"
            )
        if memory.write_mask_width is not None:
            if memory.write_mask_width != memory_byte_mask_width(
                memory.element_type.width
            ):
                raise CanonicalizationError(
                    "canonical memory write-mask width is incorrect"
                )
        if memory.write_mask is not None and memory.write_mask_width is None:
            raise CanonicalizationError(
                "canonical memory write mask has no width metadata"
            )
        scheduled = memory.read_address is None
        if scheduled and memory.write_mask is not None:
            raise CanonicalizationError(
                "canonical scheduled memory stores masks on write actions"
            )
        if not scheduled and (
            (memory.write_mask_width is None) != (memory.write_mask is None)
        ):
            raise CanonicalizationError(
                "canonical global masked memory requires a write-mask expression"
            )
        if memory.read_latency not in {0, 1}:
            raise CanonicalizationError(
                "canonical memory read latency must be zero or one"
            )
        if scheduled and memory.read_latency == 0:
            raise CanonicalizationError(
                "canonical scheduled memory requires read latency one"
            )
        for label, policy in (
            ("contents", memory.contents_reset),
            ("read data", memory.read_data_reset),
        ):
            if not isinstance(policy, MemoryResetPolicy):
                raise CanonicalizationError(
                    f"canonical memory {label} reset policy is invalid"
                )
    memories = tuple(
        Memory(
            memory.name,
            memory.semantic_id,
            memory.element_type,
            memory.depth,
            memory.read_latency,
            memory.collision,
            expressions.restore(memory.read_address) if memory.read_address is not None else None,
            expressions.restore(memory.write_enable) if memory.write_enable is not None else None,
            expressions.restore(memory.write_address) if memory.write_address is not None else None,
            expressions.restore(memory.write_data) if memory.write_data is not None else None,
            memory.source_origin,
            write_mask_width=memory.write_mask_width,
            write_mask=(
                expressions.restore(memory.write_mask)
                if memory.write_mask is not None else None
            ),
            contents_reset=memory.contents_reset,
            read_data_reset=memory.read_data_reset,
        )
        for memory in module.memories
    )
    resolved_transition = (
        ResolvedTransition(
            module.resolved_transition.semantic_id,
            module.resolved_transition.domain,
            module.resolved_transition.reset,
            module.resolved_transition.resources,
            tuple(
                ActionGroup(
                    group.semantic_id, group.rule_name,
                    restore_guard(
                        group.guard,
                        f"action group '{group.rule_name}'",
                    ),
                    tuple(
                        StateAction(
                            action.semantic_id, action.resource_id, action.kind,
                            tuple(expressions.restore(item) for item in action.operands),
                            action.owner_group, action.source_origin,
                            restore_activation(
                                action.activation,
                                f"state action '{action.semantic_id}'",
                            ),
                        ) for action in group.actions
                    ),
                    group.source_origin,
                ) for group in module.resolved_transition.action_groups
            ),
            module.resolved_transition.priorities,
        )
        if module.resolved_transition is not None else None
    )
    if len({memory.semantic_id for memory in memories}) != len(memories):
        raise CanonicalizationError("canonical memories have duplicate semantic identities")
    for memory in memories:
        if (
            memory.write_mask is not None
            and memory.write_mask.type != BitsType(memory.write_mask_width)
        ):
            raise CanonicalizationError(
                "canonical memory write-mask expression has incorrect type"
            )

    def activation_requirements(
        value: expr.Expression,
    ) -> tuple[expr.Expression, ...]:
        """Return predicates that must be true when ``value`` is true.

        Semantic nested-action lowering builds activation paths from bitwise
        conjunctions and exact ``predicate == 0`` false-arm terms.  Retaining
        every conjunction subtree as well as its leaves lets restoration prove
        the original opposite-arm relation even when a source guard itself was
        a conjunction.  This is a sound structural proof, not general Boolean
        simplification.
        """

        result = [value]
        if (
            isinstance(value, expr.Binary)
            and value.operator is expr.BinaryOperator.BIT_AND
            and value.type == BitType()
        ):
            result.extend(activation_requirements(value.left))
            result.extend(activation_requirements(value.right))
        return tuple(result)

    def is_zero_test_of(
        candidate: expr.Expression,
        original: expr.Expression,
    ) -> bool:
        if not (
            isinstance(candidate, expr.Binary)
            and candidate.operator is expr.BinaryOperator.EQUAL
            and candidate.type == BitType()
        ):
            return False
        pairs = (
            (candidate.left, candidate.right),
            (candidate.right, candidate.left),
        )
        return any(
            isinstance(zero, expr.Constant)
            and zero.type == BitType()
            and zero.value == 0
            and operand == original
            for zero, operand in pairs
        )

    def activations_are_structurally_exclusive(
        left: expr.Expression | None,
        right: expr.Expression | None,
    ) -> bool:
        if left is None or right is None:
            return False
        if (
            isinstance(left, expr.Constant)
            and left.type == BitType()
            and left.value == 0
        ) or (
            isinstance(right, expr.Constant)
            and right.type == BitType()
            and right.value == 0
        ):
            return True
        left_requirements = activation_requirements(left)
        right_requirements = activation_requirements(right)
        return any(
            is_zero_test_of(first, second)
            or is_zero_test_of(second, first)
            for first in left_requirements
            for second in right_requirements
        )

    if resolved_transition is not None:
        rules_by_name = {rule.name: rule for rule in rules}
        if len(rules_by_name) != len(rules):
            raise CanonicalizationError(
                "canonical module has duplicate rule names"
            )
        resource_by_id = {
            resource.semantic_id: resource
            for resource in resolved_transition.resources
        }
        if len(resource_by_id) != len(resolved_transition.resources):
            raise CanonicalizationError(
                "canonical transition has duplicate state-resource identities"
            )
        group_ids = [
            group.semantic_id for group in resolved_transition.action_groups
        ]
        group_names = [
            group.rule_name for group in resolved_transition.action_groups
        ]
        if len(group_ids) != len(set(group_ids)) or len(group_names) != len(set(group_names)):
            raise CanonicalizationError(
                "canonical transition has duplicate action-group identity"
            )
        if set(group_names) != set(rules_by_name):
            raise CanonicalizationError(
                "canonical transition action groups do not match typed rules"
            )
        declared_priorities = tuple(
            (priority.higher, priority.lower)
            for priority in module.rule_priorities
        )
        if len(declared_priorities) != len(set(declared_priorities)):
            raise CanonicalizationError(
                "canonical module has duplicate rule priority"
            )
        for higher, lower in declared_priorities:
            if higher not in rules_by_name or lower not in rules_by_name:
                raise CanonicalizationError(
                    "canonical rule priority references an unknown rule"
                )
            if higher == lower:
                raise CanonicalizationError(
                    "canonical rule priority cannot reference itself"
                )
        if resolved_transition.priorities != tuple(sorted(declared_priorities)):
            raise CanonicalizationError(
                "canonical transition priorities do not match typed rule priorities"
            )
        try:
            ordered_groups(resolved_transition)
        except ValueError as error:
            raise CanonicalizationError(
                "canonical rule priority graph contains a cycle"
            ) from error
        action_ids: set[str] = set()
        memory_action_resources: set[str] = set()
        output_action_resources: set[str] = set()
        output_ports = {
            port.name: port for port in module.ports
            if (
                port.direction is PortDirection.OUTPUT
                and port.protocol is InterfaceProtocol.WIRE
                and isinstance(
                    port.type, (BitType, UIntType, SIntType, BitsType)
                )
            )
        }
        for group in resolved_transition.action_groups:
            for action in group.actions:
                if action.semantic_id in action_ids:
                    raise CanonicalizationError(
                        "canonical transition has duplicate state-action identities"
                    )
                action_ids.add(action.semantic_id)
                if action.owner_group != group.semantic_id:
                    raise CanonicalizationError(
                        "canonical state action owner does not match its action group"
                    )
                resource = resource_by_id.get(action.resource_id)
                if resource is None:
                    raise CanonicalizationError(
                        f"canonical action '{action.semantic_id}' references missing resource"
                    )
                if action.activation is not None and action.activation.type != BitType():
                    raise CanonicalizationError(
                        "canonical state-action activation must have type bit"
                    )
                if action.kind is StateActionKind.OUTPUT_WRITE:
                    port = output_ports.get(resource.name)
                    if resource.kind is not StateResourceKind.OUTPUT or port is None:
                        raise CanonicalizationError(
                            "canonical output action links to a non-output resource"
                        )
                    if (
                        resource.type != port.type
                        or resource.domain != (port.domain or resolved_transition.domain)
                        or resource.depth is not None
                    ):
                        raise CanonicalizationError(
                            f"scheduled output '{resource.name}' resource metadata disagrees"
                        )
                    if len(action.operands) != 1 or action.operands[0].type != resource.type:
                        raise CanonicalizationError(
                            "canonical output write has incorrect operands"
                        )
                    output_action_resources.add(resource.semantic_id)
                elif resource.kind is StateResourceKind.OUTPUT:
                    raise CanonicalizationError(
                        "canonical output resource has a non-output action kind"
                    )
                elif action.kind is StateActionKind.REGISTER_WRITE:
                    register = register_symbols.get(resource.name)
                    if (
                        resource.kind is not StateResourceKind.REGISTER
                        or register is None
                    ):
                        raise CanonicalizationError(
                            "canonical register action links to a non-register resource"
                        )
                    if (
                        resource.type != register.type
                        or resource.domain
                        != (register.domain or resolved_transition.domain)
                    ):
                        raise CanonicalizationError(
                            f"scheduled register '{resource.name}' resource metadata disagrees"
                        )
                    if (
                        len(action.operands) != 1
                        or action.operands[0].type != resource.type
                    ):
                        raise CanonicalizationError(
                            "canonical register write has incorrect operands"
                        )
                elif resource.kind is StateResourceKind.REGISTER:
                    raise CanonicalizationError(
                        "canonical register resource has a non-register action kind"
                    )
                elif action.kind in {
                    StateActionKind.FIFO_PUSH,
                    StateActionKind.FIFO_POP,
                }:
                    fifo = next(
                        (item for item in module.fifos if item.name == resource.name),
                        None,
                    )
                    if resource.kind is not StateResourceKind.FIFO or fifo is None:
                        raise CanonicalizationError(
                            "canonical FIFO action links to a non-FIFO resource"
                        )
                    if (
                        resource.type != fifo.element_type
                        or resource.depth != fifo.depth
                        or resource.domain != resolved_transition.domain
                    ):
                        raise CanonicalizationError(
                            f"scheduled FIFO '{resource.name}' resource metadata disagrees"
                        )
                    expected_arity = (
                        1 if action.kind is StateActionKind.FIFO_PUSH else 0
                    )
                    if len(action.operands) != expected_arity or (
                        expected_arity == 1
                        and action.operands[0].type != resource.type
                    ):
                        raise CanonicalizationError(
                            "canonical FIFO action has incorrect operands"
                        )
                elif resource.kind is StateResourceKind.FIFO:
                    raise CanonicalizationError(
                        "canonical FIFO resource has a non-FIFO action kind"
                    )
                elif action.kind in {
                    StateActionKind.MEMORY_READ_REQUEST,
                    StateActionKind.MEMORY_WRITE,
                }:
                    memory = next(
                        (item for item in memories if item.semantic_id == resource.semantic_id),
                        None,
                    )
                    if memory is None or not memory.scheduled:
                        raise CanonicalizationError(
                            "canonical memory action does not link to one scheduled memory"
                        )
                    if resource.kind is not StateResourceKind.MEMORY:
                        raise CanonicalizationError(
                            "canonical memory action links to a non-memory resource"
                        )
                    expected_arity = (
                        1 if action.kind is StateActionKind.MEMORY_READ_REQUEST
                        else 3 if memory.write_mask_width is not None else 2
                    )
                    if len(action.operands) != expected_arity:
                        raise CanonicalizationError(
                            "canonical memory action has incorrect operands"
                        )
                    if action.operands[0].type != UIntType(memory.address_width):
                        raise CanonicalizationError(
                            "canonical memory action has incorrect address type"
                        )
                    if action.kind is StateActionKind.MEMORY_WRITE and action.operands[1].type != memory.element_type:
                        raise CanonicalizationError(
                            "canonical memory write has incorrect data type"
                        )
                    if (
                        expected_arity == 3
                        and action.operands[2].type != BitsType(memory.write_mask_width)
                    ):
                        raise CanonicalizationError(
                            "canonical memory write has incorrect mask type"
                        )
                    memory_action_resources.add(resource.semantic_id)
                elif resource.kind is StateResourceKind.MEMORY:
                    raise CanonicalizationError(
                        "canonical memory resource has a non-memory action kind"
                    )
            for index, left in enumerate(group.actions):
                for right in group.actions[index + 1:]:
                    if not actions_conflict(left, right):
                        continue
                    if activations_are_structurally_exclusive(
                        left.activation,
                        right.activation,
                    ):
                        continue
                    raise CanonicalizationError(
                        f"canonical action group '{group.rule_name}' has "
                        "overlapping conflicting effects"
                    )
        for group in resolved_transition.action_groups:
            rule = rules_by_name[group.rule_name]
            if group.guard != rule.guard:
                raise CanonicalizationError(
                    f"canonical action group '{group.rule_name}' guard does not "
                    "match its typed rule"
                )
            expected_effects = tuple(
                (
                    StateActionKind.REGISTER_WRITE
                    if isinstance(action.target, Register)
                    else StateActionKind.OUTPUT_WRITE,
                    action.target.name,
                    action.expression,
                    action.activation,
                )
                for action in rule.actions
            )
            actual_effects = tuple(
                (
                    action.kind,
                    resource_by_id[action.resource_id].name,
                    action.operands[0],
                    action.activation,
                )
                for action in group.actions
                if action.kind in {
                    StateActionKind.REGISTER_WRITE,
                    StateActionKind.OUTPUT_WRITE,
                }
            )
            if actual_effects != expected_effects:
                raise CanonicalizationError(
                    f"canonical action group '{group.rule_name}' effects do not "
                    "match its typed rule"
                )
        for resource in resolved_transition.resources:
            if resource.kind is not StateResourceKind.OUTPUT:
                continue
            if resource.name not in output_ports:
                raise CanonicalizationError(
                    f"scheduled output resource '{resource.name}' has no scalar output port"
                )
            if resource.semantic_id not in output_action_resources:
                raise CanonicalizationError(
                    f"scheduled output '{resource.name}' has no linked action"
                )
        for memory in memories:
            resource = resource_by_id.get(memory.semantic_id)
            if memory.scheduled:
                if resource is None:
                    raise CanonicalizationError(
                        f"scheduled memory '{memory.name}' has no state-resource link"
                    )
                if (
                    resource.kind is not StateResourceKind.MEMORY
                    or resource.name != memory.name
                    or resource.type != memory.element_type
                    or resource.depth != memory.depth
                ):
                    raise CanonicalizationError(
                        f"scheduled memory '{memory.name}' resource metadata disagrees"
                    )
                if resource.domain != resolved_transition.domain:
                    raise CanonicalizationError(
                        f"scheduled memory '{memory.name}' resource domain disagrees"
                    )
                if resource.semantic_id not in memory_action_resources:
                    raise CanonicalizationError(
                        f"scheduled memory '{memory.name}' has no linked action"
                    )
            elif resource is not None:
                raise CanonicalizationError(
                    f"global memory '{memory.name}' has a scheduled resource link"
                )
    result = Module(
        name=module.name,
        ports=module.ports,
        assignments=assignments,
        structs=module.structs,
        enums=module.enums,
        tagged_unions=module.tagged_unions,
        functions=tuple(
            Function(
                function.name,
                function.parameters,
                function.return_type,
                expressions.restore(function.body),
                function.callee_identity,
                function.metadata,
            )
            for function in module.functions
        ),
        callable_definitions=tuple(
            Function(
                function.name,
                function.parameters,
                function.return_type,
                expressions.restore(function.body),
                function.callee_identity,
                function.metadata,
            )
            for function in module.callable_definitions
        ),
        clock=module.clock,
        reset=module.reset,
        registers=registers,
        next_assignments=next_assignments,
        request_responses=module.request_responses,
        connections=module.connections,
        csr_blocks=module.csr_blocks,
        csr_access=module.csr_access,
        rules=rules,
        rule_priorities=module.rule_priorities,
        fifos=tuple(
            Fifo(
                fifo.name,
                fifo.element_type,
                fifo.depth,
                expressions.restore(fifo.data) if fifo.data is not None else None,
                expressions.restore(fifo.push) if fifo.push is not None else None,
                expressions.restore(fifo.pop) if fifo.pop is not None else None,
                fifo.source_origin,
            )
            for fifo in module.fifos
        ),
        memories=memories,
        roms=tuple(
            Rom(
                rom.name,
                rom.semantic_id,
                rom.element_type,
                rom.depth,
                rom.address_type,
                rom.read_latency,
                tuple(expressions.restore(word) for word in rom.contents),
                expressions.restore(rom.read_address),
                rom.initialization_identity,
                rom.dependency_identity,
                rom.evaluator_schema,
                rom.content_hash,
                rom.source_origin,
            )
            for rom in module.roms
        ),
        clock_domains=module.clock_domains,
        arbiters=module.arbiters,
        contracts=tuple(
            Contract(
                contract.kind,
                contract.name,
                contract.clock,
                contract.reset,
                expressions.restore(contract.expression),
            )
            for contract in module.contracts
        ),
        verification_scopes=tuple(
            VerificationScope(
                scope.semantic_id,
                scope.name,
                scope.clock,
                scope.reset,
                tuple(
                    VerificationRequirement(
                        requirement.semantic_id,
                        requirement.name,
                        verification_expressions.restore(requirement.expression),
                        requirement.source_origin,
                    )
                    for requirement in scope.requirements
                ),
                tuple(
                    VerificationGoal(
                        goal.semantic_id,
                        goal.scope_id,
                        goal.kind,
                        goal.name,
                        verification_expressions.restore(goal.expression),
                        goal.source_origin,
                    )
                    for goal in scope.goals
                ),
                scope.source_origin,
            )
            for scope in module.verification_scopes
        ),
        pipeline_explorations=tuple(
            PipelineExploration(
                exploration.output,
                exploration.result_type,
                expressions.restore(exploration.source_expression),
                exploration.constraints,
                tuple(
                    PipelineCandidate(
                        candidate.name,
                        expressions.restore(candidate.expression),
                        candidate.tree,
                        candidate.register_placement,
                        candidate.multiplier_mapping,
                        candidate.transformations,
                        candidate.latency,
                        candidate.initiation_interval,
                        candidate.estimate,
                        candidate.cost_source,
                        candidate.violations,
                        candidate.pipeline_plan,
                    )
                    for candidate in exploration.candidates
                ),
                exploration.selected,
                exploration.search_bound,
            )
            for exploration in module.pipeline_explorations
        ),
        elastic_pipeline_regions=tuple(
            ElasticPipelineRegion(
                region.semantic_id,
                region.source_endpoint,
                region.destination_endpoint,
                region.input_type,
                region.output_type,
                expressions.restore(region.source_expression),
                region.constraints,
                tuple(
                    PipelineCandidate(
                        candidate.name,
                        expressions.restore(candidate.expression),
                        candidate.tree,
                        candidate.register_placement,
                        candidate.multiplier_mapping,
                        candidate.transformations,
                        candidate.latency,
                        candidate.initiation_interval,
                        candidate.estimate,
                        candidate.cost_source,
                        candidate.violations,
                        candidate.pipeline_plan,
                    )
                    for candidate in region.candidates
                ),
                region.selected,
                region.plan,
                region.timing,
                region.clock,
                region.reset,
                region.source_origin,
            )
            for region in module.elastic_pipeline_regions
        ),
        architecture_explorations=tuple(
            ArchitectureExploration(
                exploration.output,
                exploration.result_type,
                expressions.restore(exploration.source_expression),
                exploration.constraints,
                tuple(
                    ArchitectureCandidate(
                        candidate.name,
                        expressions.restore(candidate.expression),
                        candidate.kind,
                        candidate.parallelism,
                        candidate.add_depth,
                        candidate.multiplier_count,
                        candidate.adder_count,
                        candidate.transformations,
                        candidate.equivalence,
                        candidate.violations,
                    )
                    for candidate in exploration.candidates
                ),
                exploration.selected,
                exploration.theoretical_candidates,
                exploration.search_bound,
                exploration.budget_pruned,
                exploration.constraint_pruned,
            )
            for exploration in module.architecture_explorations
        ),
        equivalences=module.equivalences,
        locals=module.locals,
        instances=module.instances,
        parameters=module.parameters,
        instance_bindings=module.instance_bindings,
        children=module.children,
        elaborated_instances=module.elaborated_instances,
        protocol_endpoints=module.protocol_endpoints,
        hierarchical_connections=module.hierarchical_connections,
        request_response_connections=module.request_response_connections,
        protocol_schemas=module.protocol_schemas,
        aggregate_protocol_endpoints=module.aggregate_protocol_endpoints,
        aggregate_protocol_connections=module.aggregate_protocol_connections,
        library_imports=module.library_imports,
        library_dependencies=module.library_dependencies,
        generic_specializations=module.generic_specializations,
        resolved_transition=resolved_transition,
        timing_contract=module.timing_contract,
        output_timings=module.output_timings,
        instance_output_timings=module.instance_output_timings,
        root_module_identity=module.root_module_identity,
        dependency_closure=module.dependency_closure,
        module_signature=module.module_signature,
        external_contract=(
            ExternalModuleContract(
                module.external_contract.logical_name,
                module.external_contract.signature,
                module.external_contract.model_callee_identity,
                module.external_contract.semantic_identity,
                module.external_contract.source_origin,
            )
            if module.external_contract is not None else None
        ),
        specialization_bindings=module.specialization_bindings,
    )
    try:
        validate_hierarchical_connections(result)
        validate_instance_port_bindings(result)
    except HierarchyError as error:
        raise CanonicalizationError(str(error)) from error
    return result


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
                attribute("memory"), attribute("signal"), node.type
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
            restored = expr.InstanceOutputRef(attribute("instance"), attribute("port"), node.type)
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
            )
            if pipeline_plan is not None and pipeline_plan.scheduler != "legacy":
                from zlang.ir.signed_reductions import expression_semantic_identity
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


def _target_kind(target: Port | RequestResponseInterface | Register) -> TargetKind:
    if isinstance(target, Port):
        return TargetKind.PORT
    if isinstance(target, RequestResponseInterface):
        return TargetKind.REQUEST_RESPONSE
    if isinstance(target, Register):
        return TargetKind.REGISTER
    raise CanonicalizationError(f"unsupported assignment target {target!r}")


def _build_entities(
    module: Module,
    functions: tuple[CanonicalFunction, ...],
    callable_definitions: tuple[CanonicalFunction, ...],
    assignments: tuple[CanonicalAssignment, ...],
    registers: tuple[CanonicalRegister, ...],
    next_assignments: tuple[CanonicalNextAssignment, ...],
    rules: tuple[CanonicalRule, ...],
    fifos: tuple[CanonicalFifo, ...],
    memories: tuple[CanonicalMemory, ...],
    roms: tuple[CanonicalRom, ...],
    contracts: tuple[CanonicalContract, ...],
    pipeline_explorations: tuple[CanonicalPipelineExploration, ...],
    architecture_explorations: tuple[
        CanonicalArchitectureExploration, ...
    ],
) -> tuple[CanonicalEntity, ...]:
    entities: list[CanonicalEntity] = [
        CanonicalEntity(
            f"architecture:module:{module.name}",
            NodeCategory.ARCHITECTURE,
            "module",
            module.name,
        )
    ]
    for struct in module.structs:
        entities.append(
            CanonicalEntity(
                f"architecture:struct:{struct.name}",
                NodeCategory.ARCHITECTURE,
                "struct",
                struct.name,
            )
        )
    for enum in module.enums:
        entities.append(
            CanonicalEntity(
                f"architecture:enum:{enum.declaration_identity}",
                NodeCategory.ARCHITECTURE,
                "enum",
                enum.name,
                details=(("members", ",".join(enum.members)),),
            )
        )
    for domain in module.clock_domains:
        entities.append(
            CanonicalEntity(
                f"architecture:clock_domain:{domain.clock}",
                NodeCategory.ARCHITECTURE,
                "clock_domain",
                domain.clock,
                details=(("reset", domain.reset),),
            )
        )
    for port in module.ports:
        category = (
            NodeCategory.VALUE
            if port.protocol is InterfaceProtocol.WIRE
            else NodeCategory.PROTOCOL
        )
        entities.append(
            CanonicalEntity(
                f"{category.value}:port:{port.name}",
                category,
                "port",
                port.name,
                details=(("protocol", port.protocol.value),),
            )
        )
    for function in functions:
        entities.append(
            CanonicalEntity(
                f"value:function:{function.name}",
                NodeCategory.VALUE,
                "function",
                function.name,
                (function.body,),
            )
        )
    for function in callable_definitions:
        entities.append(
            CanonicalEntity(
                f"value:callable:{function.callee_identity}",
                NodeCategory.VALUE,
                "callable_definition",
                function.name,
                (function.body,),
                details=(
                    ("callee_identity", function.callee_identity),
                    (
                        "kind",
                        function.metadata.kind.value
                        if function.metadata is not None else "function",
                    ),
                ),
            )
        )
    for index, assignment in enumerate(assignments):
        target = _find_assignment_target(module, assignment)
        category = (
            NodeCategory.TRANSACTION
            if isinstance(target, RequestResponseInterface)
            else NodeCategory.PROTOCOL
            if isinstance(target, Port)
            and target.protocol is not InterfaceProtocol.WIRE
            else NodeCategory.VALUE
        )
        suffix = assignment.signal.value if assignment.signal is not None else "value"
        if assignment.channel is not None:
            suffix = f"{assignment.channel.value}.{suffix}"
        entities.append(
            CanonicalEntity(
                f"{category.value}:assignment:{assignment.target_name}:{suffix}:{index}",
                category,
                "assignment",
                f"{assignment.target_name}.{suffix}",
                (assignment.expression,),
            )
        )
    for register in registers:
        entities.append(
            CanonicalEntity(
                f"state:register:{register.name}",
                NodeCategory.STATE,
                "register",
                register.name,
                (register.initial,),
            )
        )
    for index, assignment in enumerate(next_assignments):
        entities.append(
            CanonicalEntity(
                f"state:next:{assignment.target_name}:{index}",
                NodeCategory.STATE,
                "next_assignment",
                assignment.target_name,
                tuple(
                    item for item in (
                        assignment.activation, assignment.expression,
                    ) if item is not None
                ),
            )
        )
    for rule in rules:
        entities.append(
            CanonicalEntity(
                f"transaction:rule:{rule.name}",
                NodeCategory.TRANSACTION,
                "rule",
                rule.name,
                (
                    rule.guard,
                    *(
                        item
                        for action in rule.actions
                        for item in (action.activation, action.expression)
                        if item is not None
                    ),
                ),
            )
        )
    for priority in module.rule_priorities:
        entities.append(
            CanonicalEntity(
                f"transaction:priority:{priority.higher}:{priority.lower}",
                NodeCategory.TRANSACTION,
                "rule_priority",
                f"{priority.higher}>{priority.lower}",
            )
        )
    for interface in module.request_responses:
        entities.append(
            CanonicalEntity(
                f"transaction:request_response:{interface.name}",
                NodeCategory.TRANSACTION,
                "request_response",
                interface.name,
            )
        )
    for index, connection in enumerate(module.connections):
        entities.append(
            CanonicalEntity(
                f"protocol:connection:{connection.source.name}:{connection.destination.name}:{index}",
                NodeCategory.PROTOCOL,
                "connection",
                f"{connection.source.name}->{connection.destination.name}",
            )
        )
    for block in module.csr_blocks:
        entities.append(
            CanonicalEntity(
                f"architecture:csr:{block.name}",
                NodeCategory.ARCHITECTURE,
                "csr",
                block.name,
            )
        )
    for fifo in fifos:
        entities.append(
            CanonicalEntity(
                f"state:fifo:{fifo.name}",
                NodeCategory.STATE,
                "fifo",
                fifo.name,
                tuple(item for item in (fifo.data, fifo.push, fifo.pop) if item is not None),
            )
        )
    for memory in memories:
        entities.append(
            CanonicalEntity(
                f"state:memory:{memory.name}",
                NodeCategory.STATE,
                "memory",
                memory.name,
                tuple(item for item in (
                    memory.read_address,
                    memory.write_enable,
                    memory.write_address,
                    memory.write_data,
                    memory.write_mask,
                ) if item is not None),
            )
        )
    for rom in roms:
        entities.append(
            CanonicalEntity(
                f"state:rom:{rom.semantic_id}",
                NodeCategory.STATE,
                "rom",
                rom.name,
                (rom.read_address, *rom.contents),
                details=(
                    ("semantic_id", rom.semantic_id),
                    ("element_type", str(rom.element_type)),
                    ("depth", str(rom.depth)),
                    ("address_type", str(rom.address_type)),
                    ("read_latency", str(rom.read_latency)),
                    ("initialization_identity", rom.initialization_identity),
                    (
                        "dependency_identity",
                        ",".join(f"{name}:{digest}" for name, digest in rom.dependency_identity),
                    ),
                    ("evaluator_schema", rom.evaluator_schema),
                    ("content_hash", rom.content_hash),
                ),
            )
        )
    for index, arbiter in enumerate(module.arbiters):
        entities.append(
            CanonicalEntity(
                f"transaction:arbiter:{arbiter.destination.name}:{index}",
                NodeCategory.TRANSACTION,
                "packet_arbiter",
                arbiter.destination.name,
            )
        )
    for contract in contracts:
        entities.append(
            CanonicalEntity(
                f"protocol:contract:{contract.name}",
                NodeCategory.PROTOCOL,
                "contract",
                contract.name,
                (contract.expression,),
                details=(("kind", contract.kind.value),),
            )
        )
    for exploration in pipeline_explorations:
        entities.append(
            CanonicalEntity(
                f"architecture:pipeline_exploration:{exploration.output}",
                NodeCategory.ARCHITECTURE,
                "pipeline_exploration",
                exploration.output,
                tuple(
                    (
                        exploration.source_expression,
                        *(candidate.expression for candidate in exploration.candidates),
                    )
                ),
                details=(
                    ("selected", exploration.selected),
                    ("search_bound", str(exploration.search_bound)),
                    (
                        "constraints",
                        ",".join(
                            constraint.render()
                            for constraint in exploration.constraints
                        ),
                    ),
                ),
            )
        )
    for exploration in architecture_explorations:
        entities.append(
            CanonicalEntity(
                f"architecture:architecture_exploration:{exploration.output}",
                NodeCategory.ARCHITECTURE,
                "architecture_exploration",
                exploration.output,
                tuple(
                    (
                        exploration.source_expression,
                        *(candidate.expression for candidate in exploration.candidates),
                    )
                ),
                details=(
                    ("selected", exploration.selected),
                    ("search_bound", str(exploration.search_bound)),
                    (
                        "constraints",
                        ",".join(
                            constraint.render()
                            for constraint in exploration.constraints
                        ),
                    ),
                ),
            )
        )
    return tuple(entities)


def _find_assignment_target(
    module: Module,
    assignment: CanonicalAssignment,
) -> Port | RequestResponseInterface:
    candidates: tuple[Port | RequestResponseInterface, ...] = (
        *module.ports,
        *module.request_responses,
    )
    for candidate in candidates:
        if candidate.name == assignment.target_name:
            return candidate
    raise CanonicalizationError(
        f"canonical assignment target '{assignment.target_name}' is absent"
    )
