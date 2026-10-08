"""Canonical module lowering from semantic IR."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir.module import Module
from zlang.opt.entity_lowering import _build_entities, _target_kind
from zlang.opt.expression_lowering import _ExpressionBuilder
from zlang.opt.ir import (
    CanonicalActionGroup,
    CanonicalAssignment,
    CanonicalContract,
    CanonicalElasticPipelineRegion,
    CanonicalEntity,
    CanonicalExternalModuleContract,
    CanonicalFifo,
    CanonicalFunction,
    CanonicalMemory,
    CanonicalMemoryPort,
    CanonicalModule,
    CanonicalNextAssignment,
    CanonicalPipelineCandidate,
    CanonicalPipelineExploration,
    CanonicalRegister,
    CanonicalResolvedTransition,
    CanonicalRom,
    CanonicalRule,
    CanonicalStateAction,
    CanonicalVerificationGoal,
    CanonicalVerificationRequirement,
    CanonicalVerificationScope,
    OptimizationStage,
)


def _without_expression_host_evidence(module: Module) -> Module:
    """Remove compilation-local DAG evidence from retained semantic children."""

    return replace(
        module,
        children=tuple(
            _without_expression_host_evidence(child) for child in module.children
        ),
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
        selected_value_normalization_statistics=None,
    )


def lower(
    module: Module,
    *,
    stage: OptimizationStage = OptimizationStage.HIGH_LEVEL,
) -> CanonicalModule:
    """Normalize semantic IR into a deterministic canonical DAG."""

    builder = _ExpressionBuilder(module)
    functions = tuple(
        CanonicalFunction(
            name=function.name,
            parameters=function.parameters,
            return_type=function.return_type,
            body=builder.lower(function.body, f"function:{function.name}"),
            callee_identity=function.callee_identity,
            metadata=function.metadata,
        )
        for function in module.functions
    )
    callable_definitions = tuple(
        CanonicalFunction(
            name=function.name,
            parameters=function.parameters,
            return_type=function.return_type,
            body=builder.lower(
                function.body,
                f"callable:{function.callee_identity}",
            ),
            callee_identity=function.callee_identity,
            metadata=function.metadata,
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
            (
                builder.lower(register.initial)
                if register.initial is not None else None
            ),
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
            rule.domain,
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
            fifo.domain,
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
                    group.domain,
                ) for group in module.resolved_transition.action_groups
            ),
            module.resolved_transition.priorities,
        )
        if module.resolved_transition is not None else None
    )
    memories = tuple(
        CanonicalMemory(
            name=memory.name,
            semantic_id=memory.semantic_id,
            element_type=memory.element_type,
            depth=memory.depth,
            read_latency=memory.read_latency,
            collision=memory.collision,
            read_address=(
                builder.lower(memory.read_address)
                if memory.read_address is not None else None
            ),
            write_enable=(
                builder.lower(memory.write_enable)
                if memory.write_enable is not None else None
            ),
            write_address=(
                builder.lower(memory.write_address)
                if memory.write_address is not None else None
            ),
            write_data=(
                builder.lower(memory.write_data)
                if memory.write_data is not None else None
            ),
            source_origin=memory.source_origin,
            write_mask_width=memory.write_mask_width,
            write_mask=(
                builder.lower(memory.write_mask)
                if memory.write_mask is not None else None
            ),
            contents_reset=memory.contents_reset,
            read_data_reset=memory.read_data_reset,
            domain=memory.domain,
            ports=tuple(
                CanonicalMemoryPort(
                    port.name,
                    port.semantic_id,
                    port.kind,
                    port.domain,
                    builder.lower(port.address),
                    builder.lower(port.read_enable) if port.read_enable is not None else None,
                    builder.lower(port.write_enable) if port.write_enable is not None else None,
                    builder.lower(port.write_data) if port.write_data is not None else None,
                    builder.lower(port.write_mask) if port.write_mask is not None else None,
                    port.source_origin,
                )
                for port in memory.ports
            ),
            async_memory=memory.async_memory,
            write_priority=memory.write_priority,
            initial_value=(
                builder.lower(memory.initial_value)
                if memory.initial_value is not None else None
            ),
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
            rom.domain,
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
            region.temporal_graph,
        )
        for region in module.elastic_pipeline_regions
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
    )
    return _build_canonical_module(
        module=module,
        stage=stage,
        builder=builder,
        verification_builder=verification_builder,
        functions=functions,
        callable_definitions=callable_definitions,
        assignments=assignments,
        registers=registers,
        next_assignments=next_assignments,
        rules=rules,
        fifos=fifos,
        memories=memories,
        roms=roms,
        contracts=contracts,
        pipeline_explorations=pipeline_explorations,
        entities=entities,
        resolved_transition=resolved_transition,
        elastic_pipeline_regions=elastic_pipeline_regions,
        verification_scopes=verification_scopes,
    )


def _build_canonical_module(
    *,
    module: Module,
    stage: OptimizationStage,
    builder: _ExpressionBuilder,
    verification_builder: _ExpressionBuilder,
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
    entities: tuple[CanonicalEntity, ...],
    resolved_transition: CanonicalResolvedTransition | None,
    elastic_pipeline_regions: tuple[CanonicalElasticPipelineRegion, ...],
    verification_scopes: tuple[CanonicalVerificationScope, ...],
) -> CanonicalModule:
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
            equivalences=module.equivalences,
            locals=module.locals,
            instances=module.instances,
            parameters=module.parameters,
            instance_bindings=module.instance_bindings,
            children=tuple(
                _without_expression_host_evidence(child) for child in module.children
            ),
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
