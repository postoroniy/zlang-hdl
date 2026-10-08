"""Name resolution and type checking for ZLang HDL."""

from __future__ import annotations

from zlang.ast import nodes as ast
from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import types as ir_types
from zlang.source import SourceSpan
from .errors import SemanticError
from . import callables as semantic_callables
from . import module_pipeline
from . import module_interfaces as semantic_module_interfaces
from . import observations as _observations
from . import expression_coercion
from . import storage_validation as storage_validation
from .storage_symbols import FifoSymbol, MemorySymbol, RomSymbol
from . import symbols as semantic_symbols
from . import type_resolution


class StateStoragePreparer:
    """Own state symbols, aggregate interfaces, and storage declarations."""

    def prepare(
        self,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
    ) -> module_pipeline.StateStoragePreparationProduct:
        module = preparation.module
        type_resolver = preparation.type_resolver
        pure_context = preparation.expression_context
        effective_source_unit = preparation.source_unit
        struct_types = preparation.struct_types
        clock_domains = hardware.clock_domains
        clock = hardware.clock
        symbols = dict(hardware.symbols)
        ports = list(hardware.ports)
        protocol_endpoints: list[ir_module.ProtocolEndpoint] = []
        storage_declarations = storage_validation.StorageDeclarationAnalyzer().analyze(
            preparation,
            hardware,
            symbols,
            ports,
        )
        resource_symbols = storage_declarations.resource_symbols

        inputs = {
            name: port
            for name, port in symbols.items()
            if port.direction is ir_module.PortDirection.INPUT
            and port.protocol is ir_interfaces.InterfaceProtocol.WIRE
        }
        outputs = {
            name: port
            for name, port in symbols.items()
            if port.direction is ir_module.PortDirection.OUTPUT
            and port.protocol is ir_interfaces.InterfaceProtocol.WIRE
        }
        protocol_interfaces = {
            name: port
            for name, port in symbols.items()
            if port.protocol is not ir_interfaces.InterfaceProtocol.WIRE
        }
        registers: list[ir_module.Register] = []
        register_symbols: dict[str, ir_module.Register] = {}
        for declaration in module.registers:
            if declaration.name in symbols or declaration.name in register_symbols:
                raise SemanticError(f"duplicate state or port name '{declaration.name}'")
            if declaration.name in hardware.timing_names:
                raise SemanticError(
                    f"register name '{declaration.name}' conflicts with clock or reset"
                )
            register_domain = hardware.state_domains.resolve(
                "register", declaration.name, declaration.domain
            )
            type_ = type_resolver.resolve(declaration.type_name)
            initial = None
            if declaration.initial is not None:
                initial = pure_context.expressions.check_typed_boundary(
                    declaration.initial, {}, type_, pure_context
                )
                initial_analysis = semantic_callables._expand_analysis_calls(
                    initial,
                    pure_context,
                    purpose=f"register '{declaration.name}' initial value",
                )
                if initial.type != type_ or not expression_coercion.is_constant_expression(initial_analysis):
                    raise SemanticError(
                        f"initial value for register '{declaration.name}' must be a "
                        f"constant of type {type_}"
                    )
            declaration_origin = _observations.declaration_origin(
                declaration.name_origin or declaration.origin,
                f"register {declaration.name}",
                pure_context,
            )
            register = ir_module.Register(
                declaration.name,
                type_,
                initial,
                register_domain,
            )
            registers.append(register)
            register_symbols[register.name] = register
            _observations.remember_definition_target(
                pure_context,
                register,
                declaration_origin,
                name=declaration.name,
                kind="register",
            )

        module_context = pure_context.with_environment(
            clock_domains=tuple(domain.clock for domain in clock_domains),
            default_clock_domain=clock,
            generic_functions=preparation.generic_functions,
            operator_declarations=module.operators,
            struct_declarations=module.structs,
            structs=struct_types,
            parameters=preparation.parameter_values,
            unresolved_parameters=preparation.unresolved_parameter_names,
            type_resolver=type_resolver,
            generic_dependency_identity=(
                pure_context.environment.generic_dependency_identity
            ),
        ).with_scope(
            allow_delay=bool(clock_domains),
            compile_time_constants=pure_context.scope.compile_time_constants,
            static_callables=pure_context.scope.static_callables,
            candidate_site_owner=preparation.candidate_site_owner,
            source_unit=effective_source_unit,
            source_digest=preparation.source_digest,
            write_only_outputs=outputs,
            readable_cdc_outputs=set(hardware.readable_cdc_outputs),
        )
        value_symbols: dict[str, semantic_symbols.ValueSymbol] = {
            **inputs,
            **protocol_interfaces,
            **hardware.request_response_symbols,
            **register_symbols,
            **resource_symbols,
        }

        # A dotted assignment target is a semantic use of its already-resolved
        # state/protocol/resource owner.  Retain the exact base-token occurrence so
        # F12/references do not depend on parsing the target string in tooling.
        for assignment in module.assignments:
            if "." not in assignment.target or assignment.name_origin is None:
                continue
            base = assignment.target.split(".", 1)[0]
            symbol = value_symbols.get(base)
            if symbol is None:
                continue
            span = assignment.name_origin
            occurrence_span = SourceSpan(
                span.start_line,
                span.start_column,
                span.start_line,
                span.start_column + len(base),
            )
            _observations.record_definition(
                module_context,
                _observations.declaration_origin(
                    occurrence_span,
                    f"name {base}",
                    module_context,
                ),
                module_context.services.tooling.definition_targets.get(id(symbol)),
                name=base,
                kind=_observations.definition_kind(symbol),
            )

        # Aggregate protocol endpoints are expanded once, at semantic elaboration,
        # into the same leaf ports used by ordinary hierarchy.  The aggregate
        # identity is retained separately for manifests and diagnostics.
        aggregate_protocol_endpoints: list[ir_module.AggregateProtocolEndpoint] = []
        aggregate_paths: dict[str, str] = {}
        protocol_declarations = {item.name: item for item in module.protocols}
        for aggregate in module.aggregate_interfaces:
            declaration = protocol_declarations.get(aggregate.protocol)
            if declaration is None:
                raise SemanticError(f"unknown protocol '{aggregate.protocol}'")
            if aggregate.role not in declaration.roles:
                raise SemanticError(
                    f"protocol '{aggregate.protocol}' has no role '{aggregate.role}'"
                )
            arguments: dict[str, int | str] = {
                parameter.name: parameter.default
                for parameter in declaration.parameters
                if parameter.default is not None
            }
            type_arguments: dict[str, ir_types.HardwareType] = {}
            positional = 0
            for argument in aggregate.arguments:
                parameter = (
                    next((item for item in declaration.parameters if item.name == argument.name), None)
                    if argument.name is not None
                    else (declaration.parameters[positional] if positional < len(declaration.parameters) else None)
                )
                if argument.name is None:
                    positional += 1
                if parameter is None:
                    raise SemanticError(f"invalid specialization argument on protocol '{aggregate.protocol}'")
                if parameter.kind == "type":
                    value = argument.value
                    syntax = (
                        value
                        if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName))
                        else ast.TypeName(str(value))
                    )
                    type_arguments[parameter.name] = type_resolver.resolve(syntax)
                else:
                    value = argument.value
                    if isinstance(value, str):
                        parent_parameter = next((item for item in module.parameters if item.name == value), None)
                        if parent_parameter is not None and parent_parameter.default is not None:
                            value = parent_parameter.default
                    arguments[parameter.name] = value
            specialized_resolver = type_resolution.TypeResolver(
                module.type_aliases,
                module.structs,
                module.enums,
                declaration.parameters,
                arguments,
                type_bindings=type_arguments,
                identity_namespace=(
                    effective_source_unit or module.source_identity or module.name
                ),
                tagged_unions=module.tagged_unions,
            )
            members: list[ir_module.ProtocolMember] = []
            for channel in declaration.channels:
                if isinstance(channel.type_name, ast.InterfaceTypeName):
                    protocol = semantic_module_interfaces.interface_protocol(
                        channel.type_name.kind
                    )
                    payload = specialized_resolver.resolve(channel.type_name.payload_type)
                else:
                    protocol = ir_interfaces.InterfaceProtocol.WIRE
                    payload = specialized_resolver.resolve(channel.type_name)
                member_domain = channel.domain or aggregate.domain or clock
                member = ir_module.ProtocolMember(
                    channel.name, protocol, payload, channel.source_role,
                    channel.sink_role, member_domain,
                )
                members.append(member)
                synthetic = f"{aggregate.name}__{channel.name}"
                if synthetic in symbols:
                    raise SemanticError(f"aggregate member '{aggregate.name}.{channel.name}' conflicts with a port")
                direction = (
                    ir_module.PortDirection.OUTPUT
                    if aggregate.role == channel.source_role
                    else ir_module.PortDirection.INPUT
                )
                port = ir_module.Port(
                    direction, synthetic, payload,
                    protocol=protocol,
                    domain=member_domain,
                )
                symbols[synthetic] = port
                ports.append(port)
                if protocol is not ir_interfaces.InterfaceProtocol.WIRE:
                    protocol_endpoints.append(
                        ir_module.ProtocolEndpoint(
                            module.name, synthetic, direction, protocol, payload,
                            domain=member_domain,
                        )
                    )
                aggregate_paths[f"{aggregate.name}.{channel.name}"] = synthetic
            specialization_identity = (
                f"{aggregate.protocol}<" + ",".join(
                    f"{parameter.name}={type_arguments.get(parameter.name, arguments.get(parameter.name, parameter.default))}"
                    for parameter in declaration.parameters
                ) + ">"
            )
            aggregate_protocol_endpoints.append(
                ir_module.AggregateProtocolEndpoint(
                    aggregate.name, aggregate.protocol, aggregate.role,
                    tuple(members), aggregate.domain or clock, specialization_identity,
                )
            )
        module_context.scope.aggregate_paths.update(aggregate_paths)
        specialized_structs = tuple(
            member.payload_type
            for endpoint_ in aggregate_protocol_endpoints
            for member in endpoint_.members
            if isinstance(member.payload_type, ir_types.StructType)
        )
        if specialized_structs:
            struct_types = tuple({item.name: item for item in (*struct_types, *specialized_structs)}.values())
            module_context = module_context.with_environment(structs=struct_types)
        value_symbols.update({
            synthetic: symbols[synthetic]
            for synthetic in aggregate_paths.values()
        })

        resource_controls: dict[str, dict[str, ast.Expression]] = {
            name: {} for name in resource_symbols
        }
        for assignment in module.assignments:
            parts = assignment.target.split(".")
            if parts[0] not in resource_symbols:
                continue
            if len(parts) not in {2, 3}:
                raise SemanticError(
                    f"storage control target '{assignment.target}' has invalid depth"
                )
            resource = resource_symbols[parts[0]]
            field = parts[-1]
            if isinstance(resource, FifoSymbol):
                try:
                    signal = ir_storage.FifoSignal(field)
                except ValueError as error:
                    raise SemanticError(
                        f"FIFO '{resource.name}' has no field '{field}'"
                    ) from error
                writable = {
                    ir_storage.FifoSignal.DATA,
                    ir_storage.FifoSignal.PUSH,
                    ir_storage.FifoSignal.POP,
                }
            elif isinstance(resource, MemorySymbol):
                port = None
                if resource.ports:
                    if len(parts) != 3:
                        raise SemanticError(
                            f"ported memory control '{assignment.target}' must select "
                            "a named port and field"
                        )
                    port = next(
                        (item for item in resource.ports if item.name == parts[1]),
                        None,
                    )
                    if port is None:
                        raise SemanticError(
                            f"memory '{resource.name}' has no port '{parts[1]}'"
                        )
                    allowed = {
                        ast.MemoryPortKind.READ: {"address", "read_enable"},
                        ast.MemoryPortKind.WRITE: {"address", "enable", "data", "mask"},
                        ast.MemoryPortKind.READ_WRITE: {
                            "address", "read_enable", "write_enable",
                            "write_data", "write_mask",
                        },
                    }[port.kind]
                    if field not in allowed:
                        raise SemanticError(
                            f"memory port '{resource.name}.{port.name}' has no writable "
                            f"field '{field}'"
                        )
                    control_key = f"{port.name}.{field}"
                    if control_key in resource_controls[resource.name]:
                        raise SemanticError(
                            f"storage control '{assignment.target}' is assigned more than once"
                        )
                    resource_controls[resource.name][control_key] = assignment.expression
                    continue
                if len(parts) != 2:
                    raise SemanticError(
                        f"legacy memory control '{assignment.target}' selects one field"
                    )
                try:
                    signal = ir_storage.MemorySignal(field)
                except ValueError as error:
                    raise SemanticError(
                        f"memory '{resource.name}' has no field '{field}'"
                    ) from error
                writable = {
                    ir_storage.MemorySignal.READ_ADDRESS,
                    ir_storage.MemorySignal.WRITE_ENABLE,
                    ir_storage.MemorySignal.WRITE_ADDRESS,
                    ir_storage.MemorySignal.WRITE_DATA,
                    ir_storage.MemorySignal.WRITE_MASK,
                }
            else:
                assert isinstance(resource, RomSymbol)
                try:
                    signal = ir_storage.RomSignal(field)
                except ValueError as error:
                    raise SemanticError(
                        f"ROM '{resource.name}' has no field '{field}'"
                    ) from error
                writable = {ir_storage.RomSignal.READ_ADDRESS}
            if signal not in writable:
                raise SemanticError(
                    f"storage field '{assignment.target}' is read-only"
                )
            if field in resource_controls[resource.name]:
                raise SemanticError(
                    f"storage control '{assignment.target}' is assigned more than once"
                )
            resource_controls[resource.name][field] = assignment.expression

        return module_pipeline.StateStoragePreparationProduct(
            tuple(ports),
            symbols,
            tuple(protocol_endpoints),
            struct_types,
            storage_declarations,
            outputs,
            protocol_interfaces,
            tuple(registers),
            register_symbols,
            module_context,
            value_symbols,
            tuple(aggregate_protocol_endpoints),
            resource_controls,
        )
