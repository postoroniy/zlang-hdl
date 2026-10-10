"""Name resolution and type checking for ZLang HDL."""

from __future__ import annotations

from zlang.ir import csr as ir_csr
from zlang.ir import module as ir_module
from zlang.ir import external as ir_external
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir import top_abi as ir_top_abi
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin
from .errors import SemanticError
from . import context as semantic_context
from . import module_pipeline
from . import module_interfaces as semantic_module_interfaces
from . import expression_timing
from . import verification as semantic_verification
from .public_timing import analyze_public_module_timing
from .temporal_ready_valid import (
    TemporalReadyValidDependencyError,
    reject_temporal_ready_valid_dependency_cycles,
)


_VERIFICATION_IDENTITIES = semantic_verification.VerificationIdentityFinalizer()


class SemanticModuleFinalizer:
    """Build and validate the immutable semantic module product."""

    def finalize(
        self,
        context: semantic_context.AnalysisContext,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
        state_storage: module_pipeline.StateStoragePreparationProduct,
        behavior: module_pipeline.ModuleBehaviorProduct,
        hierarchy: module_pipeline.ModuleHierarchyProduct,
        *,
        hierarchy_cache: ir_hierarchy.HierarchyTraversalCache,
    ) -> ir_module.Module:
        module = preparation.module
        pure_context = preparation.expression_context
        generic_dependency_identity = preparation.generic_dependency_identity
        specialization_constant_bindings = context.specialization.constant_bindings
        specialization_callable_bindings = context.specialization.callable_bindings
        effective_source_unit = preparation.source_unit
        effective_source_digest = preparation.source_digest
        clock_domains = hardware.clock_domains
        ports = list(state_storage.ports)
        csr_blocks = list(hardware.csr_blocks)
        aggregate_protocol_endpoints = list(
            state_storage.aggregate_protocol_endpoints
        )
        fifos = list(behavior.storage.fifos)
        memories = list(behavior.storage.memories)
        roms = list(behavior.storage.roms)
        locals_ = list(behavior.locals)
        next_assignments = list(behavior.next_assignments)
        rules = list(behavior.rule_analysis.rules)
        resolved_transition = behavior.transition.transition
        child_irs = dict(behavior.instances.child_irs)
        elaborated_instances = list(behavior.instances.elaborated_instances)
        assignments = list(hierarchy.assignments.assignments)
        request_responses = list(hierarchy.request_responses)
        selected_hierarchy_cache = hierarchy_cache
        verification_product = semantic_verification.VerificationSemanticAnalyzer().analyze(
            semantic_verification.VerificationAnalysisContext(
                module=module,
                expression_context=hierarchy.contract_context,
                contract_symbols=hierarchy.contract_symbols,
                ports=state_storage.symbols,
                request_responses=hardware.request_response_symbols,
                register_symbols=state_storage.register_symbols,
                locals=tuple(locals_),
                clock_domains=clock_domains,
                source_unit=effective_source_unit,
                source_digest=effective_source_digest,
            )
        )
        for assignment in (*assignments, *next_assignments):
            expression_timing.expression_latency(assignment.expression)
            activation = getattr(assignment, "activation", None)
            if activation is not None:
                expression_timing.expression_latency(activation)
        for rule in rules:
            expression_timing.expression_latency(rule.guard)
            for action in rule.actions:
                expression_timing.expression_latency(action.expression)
                if action.activation is not None:
                    expression_timing.expression_latency(action.activation)
        for fifo in fifos:
            for control in (fifo.data, fifo.push, fifo.pop):
                if control is None:
                    continue
                latency = expression_timing.expression_latency(control)
                if latency not in {None, 0}:
                    raise SemanticError("FIFO control expressions cannot be staged")
        for group in resolved_transition.action_groups:
            for action in group.actions:
                if action.activation is not None:
                    expression_timing.expression_latency(action.activation)
                for operand in action.operands:
                    latency = expression_timing.expression_latency(operand)
                    if latency not in {None, 0}:
                        raise SemanticError("state action operands cannot be staged")
        for memory in memories:
            for control in (
                memory.read_address,
                memory.write_enable,
                memory.write_address,
                memory.write_data,
                memory.write_mask,
            ):
                if control is None:
                    continue
                latency = expression_timing.expression_latency(control)
                if latency not in {None, 0}:
                    raise SemanticError("memory control expressions cannot be staged")
        for rom in roms:
            latency = expression_timing.expression_latency(rom.read_address)
            if latency not in {None, 0}:
                raise SemanticError("ROM read-address expressions cannot be staged")

        (
            timing_contract,
            output_timings,
            instance_output_timings,
        ) = analyze_public_module_timing(
            module,
            ports=tuple(ports),
            assignments=tuple(assignments),
            locals_=tuple(locals_),
            clock_domains=clock_domains,
            child_irs=child_irs,
            elaborated_instances=tuple(elaborated_instances),
            instance_bindings=hierarchy.assignments.instance_bindings,
            request_responses=tuple(request_responses),
            aggregate_protocol_endpoints=tuple(aggregate_protocol_endpoints),
            csr_blocks=tuple(csr_blocks),
            source_unit=effective_source_unit,
            source_digest=effective_source_digest,
        )

        module_signature = semantic_module_interfaces.named_module_signature(
            module,
            resolver=preparation.type_resolver,
            specialization_type_bindings=context.specialization.type_bindings,
            actual_ports=hardware.source_scalar_ports,
            actual_clock_domains=clock_domains,
            actual_request_responses=tuple(request_responses),
            actual_aggregate_endpoints=tuple(aggregate_protocol_endpoints),
            actual_timing=timing_contract,
            source_unit=effective_source_unit,
            source_digest=effective_source_digest,
            source_digests=preparation.source_digests,
        )

        external_contract = None
        if module.external_model is not None:
            if module_signature is None or hardware.external_model_function is None:
                raise SemanticError(
                    f"external module '{module.name}' requires a resolved named interface",
                    code="ZL-EXTERN-SIGNATURE",
                )
            external_contract = ir_external.ExternalModuleContract(
                module.name,
                module_signature,
                hardware.external_model_function.callee_identity,
                source_origin=(
                    SourceOrigin(
                        module.external_origin,
                        f"external module {module.name}",
                        effective_source_unit,
                        effective_source_digest,
                    )
                    if module.external_origin is not None else None
                ),
            )

        csr_access: ir_csr.CsrAccessInterface | None = None
        if csr_blocks:
            csr_domains = {block.domain for block in csr_blocks}
            if len(csr_domains) != 1:
                raise SemanticError(
                    "all CSR blocks sharing the canonical access ABI must belong "
                    "to one clock domain",
                    code="ZL-DOMAIN-CROSSING",
                )
            csr_domain = csr_blocks[0].domain
            reserved = {"addr", "write", "wdata", "read", "rdata", "ready"}
            collision = next((port.name for port in ports if port.name in reserved), None)
            if collision is not None:
                raise SemanticError(
                    f"CSR module port '{collision}' conflicts with the canonical CSR access ABI"
                )
            csr_access = ir_csr.CsrAccessInterface(
                semantic_id=f"csr-access:{hardware.csr_module_identity}"
            )
            ports.extend(
                ir_module.Port(
                    ir_module.PortDirection.INPUT, name, type_,
                    domain=csr_domain,
                )
                for name, type_ in csr_access.input_types
            )
            ports.extend(
                ir_module.Port(
                    ir_module.PortDirection.OUTPUT, name, type_,
                    domain=csr_domain,
                )
                for name, type_ in csr_access.output_types
            )
            for block in csr_blocks:
                for binding in block.state_bindings:
                    ports.extend((
                        ir_module.Port(
                            ir_module.PortDirection.OUTPUT,
                            ir_csr.csr_state_port_name(binding),
                            binding.canonical_type,
                            domain=block.domain,
                        ),
                    ))
                for observation in block.access_observations:
                    ports.extend((
                        ir_module.Port(
                            ir_module.PortDirection.OUTPUT,
                            ir_csr.csr_read_hit_port_name(observation),
                            ir_types.BitType(),
                            domain=block.domain,
                        ),
                        ir_module.Port(
                            ir_module.PortDirection.OUTPUT,
                            ir_csr.csr_observation_write_hit_port_name(observation),
                            ir_types.BitType(),
                            domain=block.domain,
                        ),
                        ir_module.Port(
                            ir_module.PortDirection.OUTPUT,
                            ir_csr.csr_observation_write_value_port_name(observation),
                            observation.canonical_type,
                            domain=block.domain,
                        ),
                        ir_module.Port(
                            ir_module.PortDirection.OUTPUT,
                            ir_csr.csr_observation_value_port_name(observation),
                            observation.canonical_type,
                            domain=block.domain,
                        ),
                    ))
                for register in block.registers:
                    for event in register.events:
                        ports.append(ir_module.Port(
                            ir_module.PortDirection.OUTPUT,
                            ir_csr.csr_event_port_name(event),
                            event.canonical_type,
                            domain=block.domain,
                        ))
                for view in block.split_views:
                    ports.append(ir_module.Port(
                        ir_module.PortDirection.OUTPUT,
                        ir_csr.csr_split_port_name(block, view),
                        view.canonical_type,
                        domain=block.domain,
                    ))

        module_specialization_bindings = (
            *(
                pure_context.services.callable_specializer.specialization_binding(
                    parameter.name,
                    specialization_constant_bindings[parameter.name],
                    generic_dependency_identity,
                )
                for parameter in module.parameters
                if parameter.kind == "constant"
                and specialization_constant_bindings is not None
                and parameter.name in specialization_constant_bindings
            ),
            *(
                pure_context.services.callable_specializer.specialization_binding(
                    parameter.name,
                    specialization_callable_bindings[parameter.name],
                    generic_dependency_identity,
                )
                for parameter in module.parameters
                if parameter.kind == "callable"
                and specialization_callable_bindings is not None
                and parameter.name in specialization_callable_bindings
            ),
        )

        source_callable_identities = frozenset(
            function.callee_identity for function in preparation.functions
        )
        result = ir_module.Module(
            name=module.name,
            ports=tuple(ports),
            assignments=tuple(assignments),
            locals=tuple(locals_),
            structs=state_storage.struct_types,
            enums=preparation.enum_types,
            tagged_unions=preparation.tagged_union_types,
            functions=preparation.functions,
            clock=hardware.clock,
            reset=hardware.reset,
            registers=state_storage.registers,
            next_assignments=tuple(next_assignments),
            request_responses=tuple(request_responses),
            connections=hierarchy.connections.connections,
            csr_blocks=tuple(csr_blocks),
            csr_access=csr_access,
            rules=tuple(rules),
            rule_priorities=behavior.transition.priorities,
            fifos=tuple(fifos),
            memories=tuple(memories),
            roms=tuple(roms),
            clock_domains=clock_domains,
            arbiters=hardware.arbiters,
            contracts=verification_product.contracts,
            pipeline_explorations=hierarchy.assignments.pipeline_explorations,
            elastic_pipeline_regions=behavior.elastic_pipeline_regions,
            equivalences=behavior.equivalences,
            parameters=preparation.resolved_module_parameters,
            instances=behavior.instances.instances,
            instance_bindings=hierarchy.assignments.instance_bindings,
            children=tuple(
                child_irs[item.semantic_path[-1]]
                for item in elaborated_instances
                if item.semantic_path
            ),
            elaborated_instances=tuple(elaborated_instances),
            protocol_endpoints=hierarchy.protocol_endpoints,
            hierarchical_connections=hierarchy.connections.hierarchical_connections,
            request_response_connections=hierarchy.connections.request_response_connections,
            protocol_schemas=preparation.protocol_schemas,
            aggregate_protocol_endpoints=tuple(aggregate_protocol_endpoints),
            aggregate_protocol_connections=hierarchy.connections.aggregate_protocol_connections,
            library_imports=tuple(sorted(preparation.seen_imports)),
            library_dependencies=tuple(
                (item.logical_path, item.digest) for item in preparation.resolved_imports
            ),
            source_identity=module.source_identity,
            source_hash=module.source_hash,
            generic_specializations=tuple(sorted(
                pure_context.services.callables.generic_specializations,
                key=lambda item: item.identity,
            )),
            resolved_transition=resolved_transition,
            timing_contract=timing_contract,
            output_timings=output_timings,
            instance_output_timings=instance_output_timings,
            root_module_identity=preparation.active_module_identity,
            dependency_closure=context.resolution.dependency_closure,
            module_signature=module_signature,
            callable_definitions=tuple(
                pure_context.services.callables.callable_definitions[identity]
                for identity in sorted(
                    pure_context.services.callables.callable_definitions
                )
                if identity not in source_callable_identities
            ),
            external_contract=external_contract,
            specialization_bindings=module_specialization_bindings,
            verification_scopes=verification_product.scopes,
            semantic_expression_arena_statistics=(
                pure_context.services.expression_arena.statistics
            ),
            semantic_expression_provenance=(
                pure_context.services.expression_arena.provenance_table
            ),
        )
        # Verification identity finalization remains demand-driven and has one
        # compiler-owned semantic owner.
        result = _VERIFICATION_IDENTITIES.finalize(result)
        try:
            ir_top_abi.build_top_physical_abi(result)
        except ir_top_abi.TopPhysicalABIError as error:
            if error.first is None or error.second is None:
                raise SemanticError(
                    str(error),
                    code="ZL-SEMANTIC-PUBLIC-ABI",
                ) from error
            first_path = ".".join(error.first.member_path)
            second_path = ".".join(error.second.member_path)
            notes = [
                f"first logical path '{first_path}'",
                f"second logical path '{second_path}'",
            ]
            if error.first.source_origin is not None:
                notes.append(
                    "first declaration/assignment span: "
                    + error.first.source_origin.render()
                )
            raise SemanticError(
                str(error),
                code="ZL-SEMANTIC-PUBLIC-ABI-COLLISION",
                primary=error.second.source_origin,
                notes=tuple(notes),
            ) from error
        try:
            ir_hierarchy.validate_hierarchical_connections(result, cache=selected_hierarchy_cache)
            ir_hierarchy.validate_instance_port_bindings(result, cache=selected_hierarchy_cache)
        except ir_hierarchy.HierarchyError as error:
            raise SemanticError(str(error)) from error
        try:
            reject_temporal_ready_valid_dependency_cycles(result)
        except TemporalReadyValidDependencyError as error:
            raise SemanticError(str(error)) from error
        return result
