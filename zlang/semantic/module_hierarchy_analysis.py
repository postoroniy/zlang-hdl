"""Name resolution and type checking for ZLang HDL."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import module as ir_module
from zlang.ir import interfaces as ir_interfaces
from .errors import SemanticError
from . import context as semantic_context
from . import compile_time_evaluation
from .assignment_analysis import AssignmentAnalyzer
from .connection_endpoints import HierarchicalEndpointResolver
from .connection_validation import (
    ConnectionAnalyzer,
    OutputConnectivityValidator,
    ProtocolDependencyValidator,
    render_assignment_target,
)
from .hierarchical_connections import HierarchicalConnectionAnalyzer
from . import hierarchy as semantic_hierarchy
from . import module_pipeline
from . import expression_domains
from . import expression_control
from . import expression_support
from . import storage_validation as storage_validation
from . import symbols as semantic_symbols


_CONNECTION_ANALYZER = ConnectionAnalyzer()
_PROTOCOL_DEPENDENCY_VALIDATOR = ProtocolDependencyValidator()
_HIERARCHY_DEPENDENCY_VALIDATOR = (
    semantic_hierarchy.HierarchyDependencyValidator()
)
_MEMORY_DEPENDENCY_VALIDATOR = storage_validation.MemoryDependencyValidator()


class ModuleHierarchyAnalyzer:
    """Own child elaboration, bindings, connections, and output ownership."""

    def analyze(
        self,
        context: semantic_context.AnalysisContext,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
        state_storage: module_pipeline.StateStoragePreparationProduct,
        behavior: module_pipeline.ModuleBehaviorProduct,
    ) -> module_pipeline.ModuleHierarchyProduct:
        module = preparation.module
        pure_context = preparation.expression_context
        symbols = state_storage.symbols
        protocol_endpoints = list(state_storage.protocol_endpoints)
        resource_symbols = state_storage.storage_declarations.resource_symbols
        register_symbols = state_storage.register_symbols
        module_context = state_storage.expression_context
        value_symbols = state_storage.value_symbols
        outputs = state_storage.outputs
        locals_ = list(behavior.locals)
        next_assignments = list(behavior.next_assignments)
        assigned_outputs = set(behavior.assigned_outputs)
        request_responses = list(hardware.request_responses)
        request_response_symbols = hardware.request_response_symbols
        instance_product = behavior.instances
        child_irs = dict(instance_product.child_irs)

        endpoint_resolver = HierarchicalEndpointResolver(
            module=module,
            expression_context=module_context,
            symbols=symbols,
            instances=instance_product.instances,
            child_irs=child_irs,
            protocol_endpoints=protocol_endpoints,
        )

        connection_product = (
            HierarchicalConnectionAnalyzer().analyze(
                preparation,
                hardware,
                state_storage,
                behavior,
                instance_product,
                endpoint_resolver,
            )
        )
        connections = list(connection_product.connections)
        assigned_outputs = set(connection_product.assigned_outputs)

        assignment_product = AssignmentAnalyzer().analyze(
            context,
            preparation,
            hardware,
            state_storage,
            behavior,
            instance_product,
            connection_product,
            expression_control.IMPLEMENTATION_ANALYZER,
        )
        assignments = list(assignment_product.assignments)
        assigned_outputs = set(assignment_product.assigned_outputs)
        _MEMORY_DEPENDENCY_VALIDATOR.validate(
            behavior.storage.memories, tuple(locals_)
        )
        _HIERARCHY_DEPENDENCY_VALIDATOR.validate(
            child_irs,
            assignment_product.instance_bindings,
            tuple(locals_),
            memories=behavior.storage.memories,
            fifos=behavior.storage.fifos,
        )

        for connection in connections:
            if (
                connection.buffer_depth
                or connection.adapter is not None
                or connection.crossing is not None
            ):
                connection_outputs = _CONNECTION_ANALYZER.output_keys(connection)
            else:
                direct_assignments = _CONNECTION_ANALYZER.expand_direct(connection)
                connection_outputs = tuple(
                    (assignment.target.name, assignment.channel, assignment.signal)
                    for assignment in direct_assignments
                )
            for key in connection_outputs:
                if key in assigned_outputs:
                    target_name, channel, signal = key
                    target = symbols[target_name]
                    rendered = render_assignment_target(
                        target, signal, channel
                    )
                    raise SemanticError(
                        f"connection and explicit assignment both drive '{rendered}'"
                    )
                assigned_outputs.add(key)
            if (
                not connection.buffer_depth
                and connection.adapter is None
                and connection.crossing is None
            ):
                assignments.extend(direct_assignments)

        for assignment in assignments:
            if not isinstance(assignment.target, ir_module.Port):
                continue
            target_domain = assignment.target.domain
            source_domains = expression_domains.expression_domains(
                expression_support._expand_immutable_locals(
                    assignment.expression, value_symbols,
                    work_budget=module_context.services,
                ),
                {**symbols, **resource_symbols},
                register_symbols,
            )
            mismatched = {
                domain
                for domain in source_domains
                if domain is not None and domain != target_domain
            }
            if mismatched:
                source_domain = sorted(mismatched)[0]
                raise SemanticError(
                    f"implicit clock-domain crossing in assignment to "
                    f"'{assignment.target.name}' from '{source_domain}' to "
                    f"'{target_domain}' is not allowed",
                    code="ZL-DOMAIN-CROSSING",
                    primary=assignment.expression.origin,
                    fixes=("insert an explicit supported clock-domain crossing",),
                )
        for assignment in next_assignments:
            source_domains = expression_domains.expression_domains(
                expression_support._expand_immutable_locals(
                    assignment.expression, value_symbols,
                    work_budget=module_context.services,
                ),
                {**symbols, **resource_symbols},
                register_symbols,
            )
            mismatched = {
                domain
                for domain in source_domains
                if domain is not None and domain != assignment.target.domain
            }
            if mismatched:
                raise SemanticError(
                    f"implicit clock-domain crossing in next state for "
                    f"'{assignment.target.name}' is not allowed",
                    code="ZL-DOMAIN-CROSSING",
                    primary=assignment.expression.origin,
                )

        # Infer request/response ownership from the existing field assignments.
        # Requester and responder declarations are deliberately not a new syntax:
        # each owns one complete ready/valid half of the bidirectional interface.
        requester_fields = frozenset(
            {
                (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.PAYLOAD),
                (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.VALID),
                (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.READY),
            }
        )
        responder_fields = frozenset(
            {
                (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.READY),
                (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.PAYLOAD),
                (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.VALID),
            }
        )
        for index, interface in enumerate(request_responses):
            owned = frozenset(
                (assignment.channel, assignment.signal)
                for assignment in assignments
                if assignment.target is interface
                and assignment.channel is not None
                and assignment.signal is not None
            )
            if owned <= requester_fields:
                role = ir_interfaces.RequestResponseRole.REQUESTER
            elif owned <= responder_fields:
                role = ir_interfaces.RequestResponseRole.RESPONDER
            else:
                # Preserve the established diagnostic for a requester that tries
                # to drive an incoming field, while also rejecting mixed roles.
                raise SemanticError(
                    f"cannot drive incoming request/response field on interface "
                    f"'{interface.name}'; requester and responder ownership must "
                    "be disjoint"
                )
            updated = replace(interface, role=role)
            request_responses[index] = updated
            request_response_symbols[interface.name] = updated

        # Assignment targets are semantic identities, not name-only references.
        # Ownership inference replaces each request/response declaration with its
        # role-qualified value, so retarget assignments to that same canonical
        # object before the module is published.  Otherwise canonical restoration
        # correctly finds the updated interface while the original semantic module
        # still contains stale default-requester targets.
        assignments = [
            replace(
                assignment,
                target=request_response_symbols[assignment.target.name],
            )
            if isinstance(assignment.target, ir_module.RequestResponseInterface)
            else assignment
            for assignment in assignments
        ]
        assignment_product = replace(
            assignment_product,
            assignments=tuple(assignments),
            assigned_outputs=frozenset(assigned_outputs),
        )

        OutputConnectivityValidator().validate(
            outputs, state_storage.protocol_interfaces, tuple(request_responses),
            assigned_outputs, hardware.port_origins,
        )

        _PROTOCOL_DEPENDENCY_VALIDATOR.validate(tuple(assignments))

        contract_symbols: dict[str, semantic_symbols.ValueSymbol] = {**value_symbols, **outputs}
        contract_context = semantic_context.ExpressionContext(
            semantic_context.AnalysisEnvironment(
                preparation.function_signatures,
                generic_functions=preparation.generic_functions,
                operator_declarations=module.operators,
                struct_declarations=module.structs,
                structs=preparation.struct_types,
                parameters=preparation.parameter_values,
                unresolved_parameters=preparation.unresolved_parameter_names,
                type_resolver=preparation.type_resolver,
                generic_dependency_identity=(
                    pure_context.environment.generic_dependency_identity
                ),
                formal_config=pure_context.environment.formal_config,
                formal_verifier=pure_context.environment.formal_verifier,
                source_digests=preparation.source_digests,
            ),
            semantic_context.AnalysisServices(
                # Verification-only specialization is isolated from the production
                # callable catalog. A pure helper used only by an assertion must not
                # alter selected hardware identity or backend helper emission.
                callables=pure_context.services.callables.fork(),
                compile_time_real_quantize_cache=(
                    dict(pure_context.services.compile_time_real_quantize_cache)
                ),
                compile_time_budget=compile_time_evaluation.CompileTimeBudget(),
                tooling=replace(
                    pure_context.services.tooling,
                    completion_scopes=None,
                ),
            ),
            semantic_context.ExpressionScope(
                allow_delay=True,
                compile_time_constants=pure_context.scope.compile_time_constants,
                static_callables=pure_context.scope.static_callables,
                instance_outputs=dict(module_context.scope.instance_outputs),
                instance_output_protocols=dict(
                    module_context.scope.instance_output_protocols
                ),
                instance_output_domains=dict(
                    module_context.scope.instance_output_domains
                ),
                instance_protocol_outputs=dict(
                    module_context.scope.instance_protocol_outputs
                ),
                instance_csr_state_paths=dict(
                    module_context.scope.instance_csr_state_paths
                ),
                instance_arrays=dict(module_context.scope.instance_arrays),
                allow_output_reads=True,
                source_unit=preparation.source_unit,
                source_digest=preparation.source_digest,
                functional_binder_ordinals=dict(
                    pure_context.scope.functional_binder_ordinals
                ),
                next_functional_binder_ordinal=[
                    pure_context.scope.next_functional_binder_ordinal[0]
                ],
            ),
        )
        return module_pipeline.ModuleHierarchyProduct(
            connection_product,
            assignment_product,
            tuple(protocol_endpoints),
            tuple(request_responses),
            contract_symbols,
            contract_context,
        )
