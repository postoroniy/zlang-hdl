"""Name resolution and type checking for ZLang HDL."""

from __future__ import annotations

from dataclasses import replace

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr, csr as ir_csr
from zlang.ir import module as ir_module
from zlang.ir import pipelines as ir_pipelines
from zlang.ir import elastic as ir_elastic
from zlang.ir import hierarchy as ir_hierarchy
from zlang.common import stable_digest
from zlang.analysis_needs import AnalysisNeeds
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir import traversal as ir_traversal
from zlang.timing import timing_info
from zlang.ir import interfaces as ir_interfaces
from zlang import pipelines as pipelines
from .errors import SemanticError
from . import context as semantic_context
from . import callables as semantic_callables
from . import instances as semantic_instances
from . import limits as semantic_limits
from . import module_pipeline
from . import observations as _observations
from . import expression_origins
from . import expression_support
from . import equivalences as semantic_equivalences
from . import storage_validation as storage_validation
from . import state as semantic_state
from .temporal_ready_valid import select_temporal_shared_arithmetic


class ModuleBehaviorAnalyzer:
    """Own locals, state transitions, and pre-hierarchy behavior."""

    def __init__(self, recursive_analyze):
        self._recursive_analyze = recursive_analyze

    def analyze(
        self,
        context: semantic_context.AnalysisContext,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
        state_storage: module_pipeline.StateStoragePreparationProduct,
        *,
        analysis_needs: AnalysisNeeds,
        active_instance_stack: tuple[str, ...],
        hierarchy_cache: ir_hierarchy.HierarchyTraversalCache,
    ) -> module_pipeline.ModuleBehaviorProduct:
        module = preparation.module
        type_resolver = preparation.type_resolver
        clock_domains = hardware.clock_domains
        clock = hardware.clock
        reset = hardware.reset
        symbols = state_storage.symbols
        ports = list(state_storage.ports)
        resource_symbols = state_storage.storage_declarations.resource_symbols
        module_context = state_storage.expression_context
        value_symbols = state_storage.value_symbols
        request_response_symbols = hardware.request_response_symbols
        storage_product = storage_validation.StorageAnalyzer().analyze(
            context,
            preparation,
            state_storage,
            evaluator_schema=semantic_limits.COMPILE_TIME_EVALUATOR_SCHEMA,
        )
        instance_specializer = semantic_instances.InstanceSpecializer(
            semantic_instances.InstanceAnalysisContext(
                module=module,
                expression_context=module_context,
                type_resolver=type_resolver,
                value_symbols=value_symbols,
                symbols=symbols,
                resource_symbols=resource_symbols,
                request_response_symbols=request_response_symbols,
                clock_domains=clock_domains,
                clock=clock,
                reset=reset,
                enum_identity_namespace=preparation.enum_identity_namespace,
                source_unit=preparation.source_unit,
                source_digest=preparation.source_digest,
            )
        )
        instance_product = semantic_instances.InstanceElaborator(
            semantic_instances.InstanceElaborationContext(
                specializer=instance_specializer,
                recursive_analyze=self._recursive_analyze,
                analysis=context,
                preparation=preparation,
                active_instance_stack=active_instance_stack,
                hierarchy_cache=hierarchy_cache,
                analysis_needs=analysis_needs,
            )
        ).analyze()
        constant_local_cache = instance_specializer.constant_locals
        # Locals are pure bindings, but next-state expressions may use them.  Make
        # the bindings available before checking register transitions; the later
        # source-order pass skips names already elaborated here.
        locals_: list[ir_module.LocalValue] = [
            constant_local_cache[item.target]
            for item in module.assignments
            if item.target in constant_local_cache
        ]
        for declaration in module.assignments:
            if "." in declaration.target or declaration.target in symbols or declaration.target in resource_symbols or declaration.target in request_response_symbols:
                continue
            if declaration.target in {item.name for item in locals_}:
                continue
            expected = type_resolver.resolve(declaration.type_name) if declaration.type_name is not None else None
            value = (
                module_context.expressions.check_typed_boundary(
                    declaration.expression, value_symbols, expected, module_context
                )
                if expected is not None
                else module_context.expressions.check(
                    declaration.expression, value_symbols, None, module_context
                )
            )
            semantic_callables._validate_tuple_destructure_assignment(declaration, value)
            if expected is not None and value.type != expected:
                raise SemanticError(f"local '{declaration.target}' has type {value.type}, expected {expected}")
            compile_time, value_range = expression_support._local_constant_and_range(
                value, module_context, name=declaration.target
            )
            local = ir_module.LocalValue(
                declaration.target,
                value.type,
                value,
                compile_time,
                value_range,
                expression_semantic_identity(value),
            )
            locals_.append(local)
            value_symbols[local.name] = local
            _observations.remember_definition_target(
                module_context,
                local,
                _observations.declaration_origin(
                    declaration.name_origin or declaration.origin,
                    f"value {declaration.target}",
                    module_context,
                ),
                name=declaration.target,
                kind="value",
            )

        next_assignments: list[ir_module.NextAssignment] = []
        assigned_registers: set[str] = set()
        for assignment in module.next_assignments:
            target = state_storage.register_symbols.get(assignment.target)
            if target is None:
                raise SemanticError(
                    f"next-state target '{assignment.target}' is not a register"
                )
            if target.name in assigned_registers:
                raise SemanticError(
                    f"register '{target.name}' has more than one next-state assignment"
                )
            _observations.record_definition(
                module_context,
                _observations.declaration_origin(
                    assignment.target_origin,
                    f"register {target.name}",
                    module_context,
                ),
                module_context.services.tooling.definition_targets.get(id(target)),
                name=target.name,
                kind="register",
            )
            expression = module_context.expressions.check_typed_boundary(
                assignment.expression, value_symbols, target.type, module_context
            )
            if expression.type != target.type:
                raise SemanticError(
                    f"next state for register '{target.name}' has type "
                    f"{expression.type}, expected {target.type}"
                )
            next_assignments.append(ir_module.NextAssignment(target, expression))
            assigned_registers.add(target.name)

        rule_product = semantic_state.RuleAnalyzer().analyze(
            preparation,
            hardware,
            state_storage,
            storage_product,
            frozenset(assigned_registers),
        )
        transition_product = semantic_state.StateTransitionAnalyzer().analyze(
            priorities=module.rule_priorities,
            rules=rule_product.rules,
            registers=state_storage.registers,
            fifos=storage_product.fifos,
            memories=storage_product.memories,
            ports=tuple(ports),
            resource_actions=rule_product.resource_actions,
            transition_prefix=storage_product.transition_prefix,
            clock=clock,
            reset=reset,
        )
        rule_output_targets = transition_product.rule_output_targets

        elastic_pipeline_regions: list[ir_elastic.ElasticPipelineRegion] = []
        assigned_outputs: set[
            tuple[str, ir_interfaces.RequestResponseChannel | None, ir_interfaces.InterfaceSignal | None]
        ] = set()
        for target_name in rule_output_targets:
            assigned_outputs.add((target_name, None, None))

        for block in hardware.csr_blocks:
            for register in block.registers:
                for field in register.fields:
                    if (
                        field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.COMMAND
                    ):
                        if field.binding.signal in rule_output_targets:
                            raise SemanticError(
                                f"output '{field.binding.signal}' is driven by both "
                                "a CSR command binding and a rule action"
                            )
                        assigned_outputs.add((field.binding.signal, None, None))
                for event in register.events:
                    if event.signal in rule_output_targets:
                        raise SemanticError(
                            f"output '{event.signal}' is driven by both a CSR event "
                            "binding and a rule action"
                        )
                    assigned_outputs.add((event.signal, None, None))

        for arbiter in hardware.arbiters:
            for source in arbiter.sources:
                assigned_outputs.add((source.name, None, ir_interfaces.PacketSignal.READY))
            assigned_outputs.update(
                {
                    (arbiter.destination.name, None, ir_interfaces.PacketSignal.PAYLOAD),
                    (arbiter.destination.name, None, ir_interfaces.PacketSignal.VALID),
                    (arbiter.destination.name, None, ir_interfaces.PacketSignal.LAST),
                }
            )

        transform_declarations = tuple(
            declaration
            for declaration in module.connections
            if declaration.transform is not None
        )
        if transform_declarations:
            if len(transform_declarations) != 1 or len(module.connections) != 1:
                raise SemanticError(
                    "the bounded elastic pipeline slice requires exactly one "
                    "ready/valid transform connection"
                )
            declaration = transform_declarations[0]
            transform = declaration.transform
            assert transform is not None
            if any((
                declaration.buffer_depth,
                declaration.request_buffer_depth,
                declaration.response_buffer_depth,
                declaration.adapter is not None,
                declaration.crossing is not None,
            )):
                raise SemanticError(
                    "an elastic pipeline transform cannot also specify buffering, "
                    "an adapter, or a crossing"
                )
            source = symbols.get(declaration.source)
            destination = symbols.get(declaration.destination)
            if source is None or destination is None:
                missing = declaration.source if source is None else declaration.destination
                raise SemanticError(f"elastic connection endpoint '{missing}' is not a port")
            if (
                source.direction is not ir_module.PortDirection.INPUT
                or destination.direction is not ir_module.PortDirection.OUTPUT
                or source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
                or destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            ):
                raise SemanticError(
                    "elastic pipeline requires one ready/valid input source and "
                    "one ready/valid output destination"
                )
            if len(ports) != 2 or any(
                port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID for port in ports
            ):
                raise SemanticError(
                    "the first elastic pipeline slice supports exactly two "
                    "ready/valid ports"
                )
            if clock is None or reset is None or len(clock_domains) != 1:
                raise SemanticError(
                    "elastic pipeline requires exactly one synchronous clock/reset domain"
                )
            physical_domain = clock_domains[0]
            if not physical_domain.is_legacy_default:
                raise SemanticError(
                    "elastic pipeline requires a supported direct-SV clock/reset "
                    "contract: rising-edge clock, synchronous active-high reset, "
                    "and unspecified power-up"
                )
            if source.domain != destination.domain or source.domain != clock:
                raise SemanticError("elastic pipeline endpoints must share the module clock domain")
            if any((
                module.registers,
                module.next_assignments,
                module.rules,
                module.fifos,
                module.memories,
                module.roms,
                module.instances,
                module.request_responses,
                module.csr_blocks,
                module.arbiters,
                module.aggregate_interfaces,
                module.timing is not None,
            )):
                raise SemanticError(
                    "elastic pipeline compiler-owned state cannot be mixed with "
                    "user state, storage, hierarchy, CSR, arbitration, aggregate "
                    "protocols, or a module timing contract"
                )
            temporal_implement = (
                transform.expression
                if isinstance(transform.expression, ast.ImplementExpr)
                else None
            )
            operand = module_context.expressions.check(
                (
                    temporal_implement.expression
                    if temporal_implement is not None
                    else transform.expression
                ),
                value_symbols,
                destination.type,
                module_context,
            )
            operand = semantic_callables._expand_analysis_calls(
                operand,
                module_context,
                purpose="exploration operand",
                functions=preparation.functions,
            )
            expression_support._validate_elastic_kernel_capture(operand, declaration.source)
            constraints = tuple(
                ir_pipelines.PipelineConstraint(
                    ir_pipelines.PipelineMetric(
                        "throughput"
                        if constraint.metric is ast.PipelineMetric.INITIATION_INTERVAL
                        else constraint.metric.value
                    ),
                    ir_pipelines.PipelineRelation(constraint.relation.value),
                    constraint.value,
                )
                for constraint in transform.constraints
            )
            temporal_graph = None
            temporal_semantic_id = None
            if temporal_implement is None:
                try:
                    exploration = pipelines.explore_pipeline(
                        f"{destination.name}.payload",
                        operand,
                        destination.type,
                        constraints,
                        module_context.allocate_delay,
                        source.domain,
                    )
                except pipelines.PipelineExplorationError as error:
                    raise SemanticError(str(error)) from error
            else:
                temporal_selection = select_temporal_shared_arithmetic(
                    temporal_implement,
                    operand,
                    source_name=source.name,
                    destination_name=destination.name,
                    destination_type=destination.type,
                    input_type=source.type,
                    transform_constraints=transform.constraints,
                    pipeline_constraints=constraints,
                    allocate_instance=module_context.allocate_delay,
                    domain=source.domain,
                )
                exploration = temporal_selection.exploration
                temporal_graph = temporal_selection.temporal_graph
                temporal_semantic_id = temporal_selection.semantic_identity
            # Elastic bookkeeping is real implementation state: one valid bit per
            # advance stage plus the bounded ready/advance control.  Preserve the
            # unchanged pipeline scheduling/deterministic cost selection rank while publishing the complete estimate.  The
            # added control cost is common to every candidate, and valid FF cost is
            # a function of latency which is already an earlier deterministic
            # tie-break dimension.
            elastic_candidates = tuple(
                replace(
                    candidate,
                    estimate=replace(
                        candidate.estimate,
                        lut=(candidate.estimate.lut + 2)
                        if temporal_graph is None else candidate.estimate.lut,
                        ff=(candidate.estimate.ff + candidate.latency)
                        if temporal_graph is None else candidate.estimate.ff,
                    ),
                )
                for candidate in exploration.candidates
            )
            selected = next(
                candidate
                for candidate in elastic_candidates
                if candidate.name == exploration.selected
            )
            expression_support._validate_elastic_kernel_capture(selected.expression, declaration.source)
            selected_timing = timing_info(selected.expression)
            if temporal_graph is None and (
                selected.latency < 1
                or selected.initiation_interval != 1
                or selected_timing.latency != selected.latency
            ):
                raise SemanticError(
                    "selected pipeline expression cannot be safely enable-gated: "
                    "its exact typed stage latency does not match the selected plan"
                )
            staged = tuple(
                node
                for node in ir_traversal.walk_expression(
                    selected.expression,
                    policy=ir_traversal.ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
                )
                if isinstance(node, ir_expr.Pipeline)
            )
            if temporal_graph is None and (not staged or any(
                isinstance(node, ir_expr.Delay)
                for node in ir_traversal.walk_expression(
                    selected.expression,
                    policy=ir_traversal.ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
                )
            )):
                raise SemanticError(
                    "elastic pipeline requires an pipeline scheduling selected Pipeline-only plan"
                )
            stage_instances = tuple(sorted((node.instance, node.stages) for node in staged))
            if temporal_graph is None and len(stage_instances) != len({
                instance for instance, _ in stage_instances
            }):
                raise SemanticError("elastic pipeline contains conflicting stage identities")
            origin = expression_origins.semantic_origin(transform, module_context)
            semantic_id = temporal_semantic_id or "elastic:" + stable_digest({
                "source": source.name,
                "destination": destination.name,
                "kernel": expression_semantic_identity(operand),
                "constraints": tuple(item.render() for item in constraints),
                "selected": selected.name,
                "latency": selected.latency,
            })
            plan = (
                None if temporal_graph is not None else ir_elastic.ElasticPipelinePlan(
                    selected.name,
                    selected.latency,
                    stage_instances,
                    selected.latency,
                )
            )
            timing = ir_elastic.ElasticTimingContract(
                selected.latency,
                selected.initiation_interval,
                1 if temporal_graph is not None else selected.latency,
                stall_policy=(
                    ir_elastic.ElasticStallPolicy.NON_INTERLEAVED_TRANSACTION
                    if temporal_graph is not None
                    else ir_elastic.ElasticStallPolicy.GLOBAL_CLOCK_ENABLE
                ),
            )
            elastic_pipeline_regions.append(ir_elastic.ElasticPipelineRegion(
                semantic_id,
                source.name,
                destination.name,
                source.type,
                destination.type,
                operand,
                exploration.constraints,
                elastic_candidates,
                exploration.selected,
                plan,
                timing,
                clock,
                reset,
                origin,
                (),
                temporal_graph,
            ))
            assigned_outputs.update({
                (source.name, None, ir_interfaces.ReadyValidSignal.READY),
                (destination.name, None, ir_interfaces.ReadyValidSignal.PAYLOAD),
                (destination.name, None, ir_interfaces.ReadyValidSignal.VALID),
            })

        return module_pipeline.ModuleBehaviorProduct(
            storage_product,
            instance_product,
            instance_specializer.known_modules,
            tuple(locals_),
            tuple(next_assignments),
            rule_product,
            transition_product,
            tuple(elastic_pipeline_regions),
            frozenset(assigned_outputs),
            semantic_equivalences.analyze_equivalences(module.equivalences),
        )
