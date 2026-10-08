"""Canonical module restoration to semantic IR."""

from __future__ import annotations

from functools import partial

from zlang.ir import expressions as expr
from zlang.ir.cdc import ClockDomain
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.elastic import ElasticPipelineRegion
from zlang.ir.external import ExternalModuleContract
from zlang.ir.hierarchy import (
    HierarchyError,
    HierarchyTraversalCache,
    validate_hierarchical_connections,
    validate_instance_port_bindings,
)
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
from zlang.ir.pipelines import PipelineCandidate, PipelineExploration
from zlang.ir.state import (
    ActionGroup,
    ResolvedTransition,
    StateAction,
    StateActionKind,
    StateResourceKind,
    actions_conflict,
    ordered_groups,
)
from zlang.ir.storage import (
    Fifo,
    Memory,
    MemoryPort,
    MemoryPortKind,
    MemoryResetPolicy,
    Rom,
    memory_byte_mask_width,
)
from zlang.ir.types import BitType, BitsType, SIntType, UIntType
from zlang.ir.verification import (
    Contract,
    VerificationGoal,
    VerificationRequirement,
    VerificationScope,
)
from zlang.opt.expression_restoration import _ExpressionRestorer
from zlang.opt.ir import CanonicalModule, NodeId, TargetKind
from zlang.opt.lowering_errors import CanonicalizationError


def _resolve_target(
    kind: TargetKind,
    name: str,
    *,
    ports: dict[str, Port],
    request_responses: dict[str, RequestResponseInterface],
    registers: dict[str, Register],
) -> Port | RequestResponseInterface | Register:
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
        return registers[name]
    except KeyError as error:
        raise CanonicalizationError(
            f"canonical target references missing register '{name}'"
        ) from error


def _restore_activation(
    expressions: _ExpressionRestorer,
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


def _restore_guard(
    expressions: _ExpressionRestorer,
    node: NodeId,
    label: str,
) -> expr.Expression:
    guard = expressions.restore(node)
    if guard.type != BitType():
        raise CanonicalizationError(
            f"canonical {label} guard must have type bit"
        )
    return guard




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

    domains_by_clock = {item.clock: item for item in module.clock_domains}
    for block in module.csr_blocks:
        if block.domain is None:
            if len(module.clock_domains) > 1:
                raise CanonicalizationError(
                    f"canonical CSR block '{block.name}' has no clock domain"
                )
            continue
        domain = domains_by_clock.get(block.domain)
        if domain is None or block.reset != domain.reset:
            raise CanonicalizationError(
                f"canonical CSR block '{block.name}' physical domain disagrees "
                "with the module clock/reset contracts"
            )
        if any(
            binding.clock_domain != block.domain
            or binding.reset_domain != block.reset
            for binding in block.state_bindings
        ):
            raise CanonicalizationError(
                f"canonical CSR block '{block.name}' state binding domain disagrees"
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
            (
                expressions.restore(register.initial)
                if register.initial is not None else None
            ),
            register.domain,
        )
        for register in module.registers
    )
    register_symbols = {register.name: register for register in registers}

    target = partial(
        _resolve_target,
        ports=ports,
        request_responses=request_responses,
        registers=register_symbols,
    )

    assignments = tuple(
        Assignment(
            target(item.target_kind, item.target_name),
            expressions.restore(item.expression),
            item.signal,
            item.channel,
        )
        for item in module.assignments
    )
    restore_activation = partial(_restore_activation, expressions)
    restore_guard = partial(_restore_guard, expressions)

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
            rule.domain,
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
        scheduled = memory.read_address is None and not memory.ports
        if memory.ports:
            if any(item is not None for item in controls) or memory.write_mask is not None:
                raise CanonicalizationError(
                    "canonical ported memory cannot also use legacy controls"
                )
            if len(memory.ports) > 8:
                raise CanonicalizationError(
                    "canonical ported memory supports at most eight logical ports"
                )
            names = tuple(port.name for port in memory.ports)
            identities = tuple(port.semantic_id for port in memory.ports)
            if len(names) != len(set(names)) or len(identities) != len(set(identities)):
                raise CanonicalizationError(
                    "canonical memory port names and identities must be unique"
                )
            writable = tuple(
                port.name for port in memory.ports
                if port.kind in {MemoryPortKind.WRITE, MemoryPortKind.READ_WRITE}
            )
            if len(writable) > 1 and (
                len(memory.write_priority) != len(writable)
                or set(memory.write_priority) != set(writable)
            ):
                raise CanonicalizationError(
                    "canonical multi-writer memory requires a complete priority"
                )
            for port in memory.ports:
                if not port.semantic_id or not port.name or not port.domain:
                    raise CanonicalizationError(
                        "canonical memory port identity, name, and domain are required"
                    )
                readable = port.kind in {MemoryPortKind.READ, MemoryPortKind.READ_WRITE}
                writable_port = port.kind in {MemoryPortKind.WRITE, MemoryPortKind.READ_WRITE}
                if readable != (port.read_enable is not None):
                    raise CanonicalizationError(
                        f"canonical memory port '{port.name}' has invalid read controls"
                    )
                if writable_port != (
                    port.write_enable is not None and port.write_data is not None
                ):
                    raise CanonicalizationError(
                        f"canonical memory port '{port.name}' has invalid write controls"
                    )
                if not writable_port and port.write_mask is not None:
                    raise CanonicalizationError(
                        f"canonical read port '{port.name}' cannot carry a write mask"
                    )
            if memory.async_memory:
                kinds = tuple(port.kind for port in memory.ports)
                if (
                    len(kinds) != 2
                    or kinds.count(MemoryPortKind.WRITE) != 1
                    or kinds.count(MemoryPortKind.READ) != 1
                    or len({port.domain for port in memory.ports}) != 2
                    or memory.read_latency < 1
                ):
                    raise CanonicalizationError(
                        "canonical async memory requires distinct one-write/one-read ports and at least one read cycle"
                    )
            elif len({port.domain for port in memory.ports}) != 1:
                raise CanonicalizationError(
                    "canonical ordinary ported memory requires one clock domain"
                )
        elif memory.async_memory or memory.write_priority:
            raise CanonicalizationError(
                "canonical async/priority metadata requires named ports"
            )
        if scheduled and memory.write_mask is not None:
            raise CanonicalizationError(
                "canonical scheduled memory stores masks on write actions"
            )
        if not scheduled and not memory.ports and (
            (memory.write_mask_width is None) != (memory.write_mask is None)
        ):
            raise CanonicalizationError(
                "canonical global masked memory requires a write-mask expression"
            )
        if not 0 <= memory.read_latency <= 16:
            raise CanonicalizationError(
                "canonical memory read latency must be in 0..16"
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
            name=memory.name,
            semantic_id=memory.semantic_id,
            element_type=memory.element_type,
            depth=memory.depth,
            read_latency=memory.read_latency,
            collision=memory.collision,
            read_address=(
                expressions.restore(memory.read_address)
                if memory.read_address is not None else None
            ),
            write_enable=(
                expressions.restore(memory.write_enable)
                if memory.write_enable is not None else None
            ),
            write_address=(
                expressions.restore(memory.write_address)
                if memory.write_address is not None else None
            ),
            write_data=(
                expressions.restore(memory.write_data)
                if memory.write_data is not None else None
            ),
            source_origin=memory.source_origin,
            write_mask_width=memory.write_mask_width,
            write_mask=(
                expressions.restore(memory.write_mask)
                if memory.write_mask is not None else None
            ),
            contents_reset=memory.contents_reset,
            read_data_reset=memory.read_data_reset,
            domain=memory.domain,
            ports=tuple(
                MemoryPort(
                    port.name,
                    port.semantic_id,
                    port.kind,
                    port.domain,
                    expressions.restore(port.address),
                    expressions.restore(port.read_enable) if port.read_enable is not None else None,
                    expressions.restore(port.write_enable) if port.write_enable is not None else None,
                    expressions.restore(port.write_data) if port.write_data is not None else None,
                    expressions.restore(port.write_mask) if port.write_mask is not None else None,
                    port.source_origin,
                )
                for port in memory.ports
            ),
            async_memory=memory.async_memory,
            write_priority=memory.write_priority,
            initial_value=(
                expressions.restore(memory.initial_value)
                if memory.initial_value is not None else None
            ),
        )
        for memory in module.memories
    )
    for memory in memories:
        if memory.initial_value is None:
            continue
        try:
            constant_runtime_value(memory.initial_value)
        except ConstantExpressionError as error:
            raise CanonicalizationError(
                f"canonical memory '{memory.name}' init value is not constant: {error}"
            ) from error
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
                    group.domain,
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


    _validate_resolved_transition(
        module=module,
        resolved_transition=resolved_transition,
        rules=rules,
        memories=memories,
        register_symbols=register_symbols,
        verification_domains=verification_domains,
    )
    return _build_restored_module(
        module=module,
        expressions=expressions,
        verification_expressions=verification_expressions,
        assignments=assignments,
        registers=registers,
        next_assignments=next_assignments,
        rules=rules,
        memories=memories,
        resolved_transition=resolved_transition,
    )

def _validate_resolved_transition(
    *,
    module: CanonicalModule,
    resolved_transition: ResolvedTransition | None,
    rules: tuple[Rule, ...],
    memories: tuple[Memory, ...],
    register_symbols: dict[str, Register],
    verification_domains: dict[str, ClockDomain],
) -> None:
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
            rule = rules_by_name[group.rule_name]
            if group.domain != rule.domain:
                raise CanonicalizationError(
                    f"canonical action group '{group.rule_name}' clock domain "
                    "does not match its typed rule"
                )
            if group.domain not in verification_domains:
                raise CanonicalizationError(
                    f"canonical action group '{group.rule_name}' references "
                    f"missing clock domain '{group.domain}'"
                )
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
                        or resource.domain != group.domain
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
                        or resource.domain != group.domain
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
                        or resource.domain != fifo.domain
                        or resource.domain != group.domain
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
                    if resource.domain != memory.domain or resource.domain != group.domain:
                        raise CanonicalizationError(
                            f"scheduled memory '{resource.name}' resource domain disagrees"
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
                if resource.domain != memory.domain:
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


def _build_restored_module(
    *,
    module: CanonicalModule,
    expressions: _ExpressionRestorer,
    verification_expressions: _ExpressionRestorer,
    assignments: tuple[Assignment, ...],
    registers: tuple[Register, ...],
    next_assignments: tuple[NextAssignment, ...],
    rules: tuple[Rule, ...],
    memories: tuple[Memory, ...],
    resolved_transition: ResolvedTransition | None,
) -> Module:
    result = Module(
        name=module.name,
        ports=module.ports,
        assignments=assignments,
        structs=module.structs,
        enums=module.enums,
        tagged_unions=module.tagged_unions,
        functions=tuple(
            Function(
                name=function.name,
                parameters=function.parameters,
                return_type=function.return_type,
                body=expressions.restore(function.body),
                callee_identity=function.callee_identity,
                metadata=function.metadata,
            )
            for function in module.functions
        ),
        callable_definitions=tuple(
            Function(
                name=function.name,
                parameters=function.parameters,
                return_type=function.return_type,
                body=expressions.restore(function.body),
                callee_identity=function.callee_identity,
                metadata=function.metadata,
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
                fifo.domain,
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
                rom.domain,
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
                (),
                region.temporal_graph,
            )
            for region in module.elastic_pipeline_regions
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
    hierarchy_cache = HierarchyTraversalCache()
    try:
        validate_hierarchical_connections(result, cache=hierarchy_cache)
        validate_instance_port_bindings(result, cache=hierarchy_cache)
    except HierarchyError as error:
        raise CanonicalizationError(str(error)) from error
    return result
