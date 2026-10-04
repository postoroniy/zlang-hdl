# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned direct connection lowering and protocol-cycle validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import types as ir_types

from . import assignment_targets
from . import expression_domains
from . import expression_coercion
from . import expression_support
from . import context as semantic_context
from . import module_pipeline
from .errors import SemanticError
from .hierarchical_connections import HierarchicalConnectionProduct
from .connection_validation import render_assignment_target


@dataclass(frozen=True)
class AssignmentAnalysisProduct:
    instance_bindings: tuple[ir_module.InstancePortBinding, ...]
    assignments: tuple[ir_module.Assignment, ...]
    pipeline_explorations: tuple[object, ...]
    assigned_outputs: frozenset[tuple[str, object | None, object | None]]


class AssignmentAnalyzer:
    """Own instance bindings and typed public-output assignment lowering."""

    def analyze(
        self,
        context: semantic_context.AnalysisContext,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
        state_storage: module_pipeline.StateStoragePreparationProduct,
        behavior: module_pipeline.ModuleBehaviorProduct,
        instances: Any,
        connections: HierarchicalConnectionProduct,
        implementation_analyzer: Any,
    ) -> AssignmentAnalysisProduct:
        module = preparation.module
        child_irs = dict(instances.child_irs)
        instance_names = set(instances.instance_names)
        instance_bindings: list[ir_module.InstancePortBinding] = []
        assignments: list[ir_module.Assignment] = []
        pipeline_explorations: list[object] = []
        assigned_outputs = set(connections.assigned_outputs)
        value_symbols = state_storage.value_symbols
        locals_ = behavior.locals
        symbols = state_storage.symbols
        resource_symbols = state_storage.storage_declarations.resource_symbols
        register_symbols = state_storage.register_symbols
        request_response_symbols = hardware.request_response_symbols
        module_context = state_storage.expression_context
        hierarchical_assignments = list(module.assignments)
        child_protocol_payload_fields: dict[
            tuple[str, str], dict[tuple[str, ...], ir_expr.Expression]
        ] = {}
        for declaration in module.instances:
            hierarchical_assignments.extend(
                ast.Assignment(f"{declaration.name}.{binding.target}", binding.expression)
                for binding in declaration.bindings
            )
        for assignment in hierarchical_assignments:
            root = assignment.target.split(".", 1)[0]
            if root in resource_symbols:
                continue
            if root in instance_names:
                parts = assignment.target.split(".")
                if len(parts) >= 3:
                    child = child_irs[root]
                    if "payload" in parts[2:-1]:
                        payload_index = parts.index("payload", 2)
                        child_name = "__".join(parts[1:payload_index])
                        child_port = next(
                            (port for port in child.ports
                             if port.name == child_name), None
                        )
                        if (
                            child_port is None
                            or child_port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
                            or child_port.direction is not ir_module.PortDirection.INPUT
                        ):
                            raise SemanticError(
                                f"'{assignment.target}' is not a writable static "
                                "child ready/valid payload field"
                            )
                        field_path = tuple(parts[payload_index + 1:])
                        field_type: ir_types.HardwareType = child_port.type
                        for field_name in field_path:
                            if not isinstance(field_type, ir_types.StructType):
                                raise SemanticError(
                                    f"'{assignment.target}' does not select a "
                                    "struct payload field"
                                )
                            selected = next(
                                (field for field in field_type.fields
                                 if field.name == field_name), None
                            )
                            if selected is None:
                                raise SemanticError(
                                    f"child payload '{child_name}' has no field "
                                    f"'{field_name}'"
                                )
                            field_type = selected.type
                        bound = module_context.expressions.check_typed_boundary(
                            assignment.expression, value_symbols, field_type,
                            module_context,
                        )
                        if bound.type != field_type:
                            raise SemanticError(
                                f"binding '{assignment.target}' has type "
                                f"{bound.type}, expected {field_type}"
                            )
                        bound_domains = {
                            item for item in expression_domains.expression_domains(
                                expression_support._expand_immutable_locals(
                                    bound, value_symbols,
                                    work_budget=module_context.services,
                                ),
                                {**symbols, **resource_symbols}, register_symbols,
                            ) if item is not None
                        }
                        if (
                            child_port.domain is not None
                            and bound_domains - {child_port.domain}
                        ):
                            foreign = sorted(bound_domains - {child_port.domain})[0]
                            raise SemanticError(
                                f"child protocol payload field '{assignment.target}' "
                                f"in domain '{child_port.domain}' reads dynamic "
                                f"value from '{foreign}'",
                                code="ZL-DOMAIN-CROSSING",
                                primary=bound.origin,
                            )
                        fields_for_port = child_protocol_payload_fields.setdefault(
                            (root, child_name), {}
                        )
                        if field_path in fields_for_port:
                            raise SemanticError(
                                f"child protocol payload field '{assignment.target}' "
                                "has multiple drivers"
                            )
                        fields_for_port[field_path] = bound
                        continue
                    try:
                        signal = ir_interfaces.ReadyValidSignal(parts[-1])
                    except ValueError as error:
                        raise SemanticError(
                            f"child protocol signal '{assignment.target}' is unknown"
                        ) from error
                    if signal is ir_interfaces.ReadyValidSignal.TRANSFER or len(parts) not in {3, 4}:
                        raise SemanticError(
                            f"child protocol binding '{assignment.target}' must select "
                            "one payload, valid, or ready signal"
                        )
                    child_name = "__".join(parts[1:-1])
                    child_port = next(
                        (port for port in child.ports if port.name == child_name), None
                    )
                    if child_port is None or child_port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID:
                        raise SemanticError(
                            f"'{assignment.target}' is not a static child "
                            "ready/valid signal"
                        )
                    writable = (
                        signal is ir_interfaces.ReadyValidSignal.READY
                        if child_port.direction is ir_module.PortDirection.OUTPUT
                        else signal in {ir_interfaces.ReadyValidSignal.PAYLOAD, ir_interfaces.ReadyValidSignal.VALID}
                    )
                    if not writable:
                        raise SemanticError(
                            f"child protocol signal '{assignment.target}' is child-owned"
                        )
                    signal_type = (
                        child_port.type if signal is ir_interfaces.ReadyValidSignal.PAYLOAD else ir_types.BitType()
                    )
                    bound = module_context.expressions.check_typed_boundary(
                        assignment.expression, value_symbols, signal_type,
                        module_context,
                    )
                    if bound.type != signal_type:
                        raise SemanticError(
                            f"binding '{assignment.target}' has type {bound.type}, "
                            f"expected {signal_type}"
                        )
                    scalar_name = ir_interfaces.ready_valid_field_name(child_name, signal)
                    if any(
                        item.instance == root and item.port == scalar_name
                        for item in instance_bindings
                    ):
                        raise SemanticError(
                            f"child protocol signal '{assignment.target}' has "
                            "multiple drivers"
                        )
                    bound_domains = {
                        item for item in expression_domains.expression_domains(
                                expression_support._expand_immutable_locals(
                                    bound, value_symbols,
                                    work_budget=module_context.services,
                                ),
                            {**symbols, **resource_symbols}, register_symbols,
                        ) if item is not None
                    }
                    if child_port.domain is not None and bound_domains - {child_port.domain}:
                        foreign = sorted(bound_domains - {child_port.domain})[0]
                        raise SemanticError(
                            f"child protocol signal '{assignment.target}' in domain "
                            f"'{child_port.domain}' reads dynamic value from '{foreign}'",
                            code="ZL-DOMAIN-CROSSING",
                            primary=bound.origin,
                        )
                    instance_bindings.append(
                        ir_module.InstancePortBinding(root, scalar_name, bound)
                    )
                    continue
                if len(parts) != 2:
                    raise SemanticError("instance binding must select one child port")
                child = child_irs[root]
                child_port = next((port for port in child.inputs if port.name == parts[1]), None)
                if child_port is None:
                    raise SemanticError(f"instance '{root}' has no input port '{parts[1]}'")
                if child_port.protocol is not ir_interfaces.InterfaceProtocol.WIRE:
                    raise SemanticError(
                        f"protocol endpoint '{assignment.target}' must use connect, "
                        "not an inline scalar binding"
                    )
                bound = module_context.expressions.check_typed_boundary(
                    assignment.expression, value_symbols, child_port.type, module_context
                )
                if bound.type != child_port.type:
                    raise SemanticError(
                        f"binding '{assignment.target}' has type {bound.type}, expected {child_port.type}"
                    )
                if any(item.instance == root and item.port == child_port.name for item in instance_bindings):
                    raise SemanticError(f"instance input '{assignment.target}' is assigned more than once")
                bound_domains = {
                    item
                    for item in expression_domains.expression_domains(
                        expression_support._expand_immutable_locals(
                            bound, value_symbols, work_budget=module_context.services
                        ),
                        {**symbols, **resource_symbols},
                        register_symbols,
                    )
                    if item is not None
                }
                if child_port.domain is not None and bound_domains - {child_port.domain}:
                    foreign = sorted(bound_domains - {child_port.domain})[0]
                    raise SemanticError(
                        f"instance input '{assignment.target}' in domain "
                        f"'{child_port.domain}' reads dynamic value from '{foreign}'",
                        code="ZL-DOMAIN-CROSSING",
                        primary=bound.origin,
                        fixes=("insert an explicit supported clock-domain crossing first",),
                    )
                instance_bindings.append(ir_module.InstancePortBinding(root, child_port.name, bound))
                continue
            if "." not in assignment.target and assignment.target in {
                item.name for item in locals_
            }:
                continue
            channel: ir_interfaces.RequestResponseChannel | None = None
            target_text = assignment.target
            for prefix in sorted(
                module_context.scope.aggregate_paths, key=len, reverse=True
            ):
                if target_text == prefix or target_text.startswith(prefix + "."):
                    target_text = (
                        module_context.scope.aggregate_paths[prefix]
                        + target_text[len(prefix):]
                    )
                    break
            if root in request_response_symbols:
                target, channel, signal, target_type = assignment_targets.resolve_request_response_target(
                    target_text, request_response_symbols
                )
            else:
                target, signal, target_type = assignment_targets.resolve_port_target(
                    target_text, symbols
                )
            target_key = (target.name, channel, signal)
            if target_key in assigned_outputs:
                rendered = render_assignment_target(
                    target, signal, channel
                )
                raise SemanticError(f"output '{rendered}' is assigned more than once")

            expression_context = (
                module_context.with_scope(allow_runtime_instance_projection=True)
                if isinstance(target, ir_module.Port)
                and target.direction is ir_module.PortDirection.OUTPUT
                and target.protocol is ir_interfaces.InterfaceProtocol.WIRE
                and signal is None
                and channel is None
                else module_context
            )
            if isinstance(assignment.expression, ast.ImplementExpr):
                if signal is not None or channel is not None:
                    raise SemanticError("implement currently requires a wire output")
                assert isinstance(target, ir_module.Port)
                implementation = implementation_analyzer.analyze_site(
                    assignment.expression,
                    target=target,
                    target_type=target_type,
                    inputs=value_symbols,
                    locals_=tuple(locals_),
                    functions=tuple(preparation.functions),
                    symbols=symbols,
                    resource_symbols=resource_symbols,
                    register_symbols=register_symbols,
                    context=module_context,
                    equivalences=tuple(behavior.equivalences),
                    formal_config=context.verification.formal_config,
                    formal_verifier=context.verification.formal_verifier,
                    candidate_site_owner=preparation.candidate_site_owner,
                    exploration_results=context.implementation.exploration_results,
                )
                expression = implementation.expression
                if implementation.pipeline_exploration is not None:
                    pipeline_explorations.append(
                        implementation.pipeline_exploration
                    )
            elif isinstance(assignment.expression, ast.ImplementationChoiceExpr):
                if signal is not None or channel is not None:
                    raise SemanticError(
                        "implementation choices currently require a wire output"
                    )
                expression_context = module_context.with_scope(
                    allow_implementation_choice=True,
                )
                expression = expression_context.expressions.check(
                    assignment.expression,
                    value_symbols,
                    target_type,
                    expression_context,
                )
            elif not isinstance(assignment.expression, ast.ImplementExpr):
                source_expression = assignment.expression
                if (
                    isinstance(source_expression, ast.PipelineExpr)
                    and source_expression.domain is None
                    and isinstance(target, ir_module.Port)
                    and target.domain is not None
                ):
                    # The explicitly clock-qualified destination is an exact
                    # contextual domain boundary.  Record that constraint before
                    # typing the pipeline so constant-only kernels remain concise
                    # in a multi-clock module without declaration-order guessing.
                    source_expression = replace(
                        source_expression, domain=target.domain
                    )
                expression = expression_context.expressions.check(
                    source_expression,
                    value_symbols,
                    target_type,
                    expression_context,
                )
            if expression_context is not module_context:
                module_context.advance_delay_allocator(
                    expression_context.scope.next_delay_instance
                )
            expression = expression_coercion.coerce_raw_target(expression, target_type)
            if expression.type != target_type:
                rendered = render_assignment_target(
                    target, signal, channel
                )
                raise SemanticError(
                    f"cannot assign {expression.type} expression to "
                    f"{target_type} output '{rendered}'",
                    code="ZL-WIDTH-ASSIGNMENT",
                    primary=expression.origin,
                    fixes=("use an explicit exact-width conversion",),
                )
            assignments.append(ir_module.Assignment(target, expression, signal, channel))
            assigned_outputs.add(target_key)

        for (child_owner, child_name), field_bindings in sorted(
            child_protocol_payload_fields.items()
        ):
            child_port = next(
                port for port in child_irs[child_owner].ports
                if port.name == child_name
            )
            scalar_name = ir_interfaces.ready_valid_field_name(
                child_name, ir_interfaces.ReadyValidSignal.PAYLOAD
            )
            if any(
                item.instance == child_owner and item.port == scalar_name
                for item in instance_bindings
            ):
                raise SemanticError(
                    f"child protocol payload '{child_owner}.{child_name}' has "
                    "both whole-value and field drivers"
                )

            def build_child_payload(
                value_type: ir_types.HardwareType, path: tuple[str, ...]
            ) -> ir_expr.Expression:
                if path in field_bindings:
                    if any(
                        candidate[:len(path)] == path and len(candidate) > len(path)
                        for candidate in field_bindings
                    ):
                        raise SemanticError(
                            f"child protocol payload '{child_owner}.{child_name}' "
                            f"has overlapping field drivers at '{'.'.join(path)}'"
                        )
                    return field_bindings[path]
                if not isinstance(value_type, ir_types.StructType):
                    raise SemanticError(
                        f"child protocol payload '{child_owner}.{child_name}' "
                        f"is missing field '{'.'.join(path)}'"
                    )
                return ir_expr.StructConstruct(
                    value_type.name,
                    tuple(
                        (field.name, build_child_payload(
                            field.type, (*path, field.name)
                        ))
                        for field in value_type.fields
                    ),
                    value_type,
                )

            instance_bindings.append(ir_module.InstancePortBinding(
                child_owner, scalar_name,
                build_child_payload(child_port.type, ()),
            ))

        # A compile-time instance array denotes a fixed set of physical children,
        # not a broadcast binding.  Validate every scalar input here so both
        # backends consume the same complete typed wiring graph.
        for array, length in module_context.scope.instance_arrays.items():
            for index in range(length):
                physical = f"{array}[{index}]"
                child = child_irs[physical]
                for port in child.inputs:
                    if port.protocol is not ir_interfaces.InterfaceProtocol.WIRE:
                        continue
                    if not any(
                        binding.instance == physical and binding.port == port.name
                        for binding in instance_bindings
                    ):
                        raise SemanticError(
                            f"instance '{physical}' input '{port.name}' has no "
                            "compile-time indexed binding"
                        )

        return AssignmentAnalysisProduct(
            tuple(instance_bindings),
            tuple(assignments),
            tuple(pipeline_explorations),
            frozenset(assigned_outputs),
        )
