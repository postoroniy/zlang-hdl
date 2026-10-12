"""Canonical module-entity construction and target restoration."""

from __future__ import annotations

from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module, Port, Register, RequestResponseInterface
from zlang.opt.ir import (
    CanonicalAssignment,
    CanonicalContract,
    CanonicalEntity,
    CanonicalFifo,
    CanonicalFunction,
    CanonicalMemory,
    CanonicalNextAssignment,
    CanonicalPipelineExploration,
    CanonicalRegister,
    CanonicalRom,
    CanonicalRule,
    NodeCategory,
    TargetKind,
)
from zlang.opt.lowering_errors import CanonicalizationError


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
                (() if register.initial is None else (register.initial,)),
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
                    memory.initial_value,
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
