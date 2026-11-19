"""Fail-closed typed feature accounting for SystemVerilog plans."""

from __future__ import annotations

from zlang.backend import module_features
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir import module as ir_module

VALUE_GROUPS = (
    module_features.ModuleFeatureGroup.ASSIGNMENTS,
    module_features.ModuleFeatureGroup.LOCALS,
)
STATE_GROUPS = (
    module_features.ModuleFeatureGroup.REGISTERS,
    module_features.ModuleFeatureGroup.NEXT_ASSIGNMENTS,
    module_features.ModuleFeatureGroup.RULES,
)
STORAGE_GROUPS = (
    module_features.ModuleFeatureGroup.FIFOS,
    module_features.ModuleFeatureGroup.MEMORIES,
    module_features.ModuleFeatureGroup.ROMS,
)
PROTOCOL_GROUPS = (
    module_features.ModuleFeatureGroup.REQUEST_RESPONSE_INTERFACES,
    module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
    module_features.ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
    module_features.ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
    module_features.ModuleFeatureGroup.CREDIT_PORTS,
    module_features.ModuleFeatureGroup.VC_CREDIT_PORTS,
)
HIERARCHY_GROUPS = (
    module_features.ModuleFeatureGroup.INSTANCES,
    module_features.ModuleFeatureGroup.CONNECTIONS,
    module_features.ModuleFeatureGroup.HIERARCHICAL_CONNECTIONS,
    module_features.ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
    module_features.ModuleFeatureGroup.REQUEST_RESPONSE_LEDGERS,
)
COMPOSED_GROUPS = (
    *STATE_GROUPS,
    *STORAGE_GROUPS,
    module_features.ModuleFeatureGroup.CSR_BLOCKS,
    *PROTOCOL_GROUPS,
    module_features.ModuleFeatureGroup.ARBITERS,
    *HIERARCHY_GROUPS,
)


def physical_boundary_groups(
    module: ir_module.Module,
) -> tuple[module_features.ModuleFeatureGroup, ...]:
    """Claim endpoint metadata backed by the renderer's physical ports."""

    ports = {port.name: port for port in module.ports}
    ready_valid_endpoints = tuple(
        ir_module.ProtocolEndpoint(
            module.name, port.name, port.direction, port.protocol, port.type,
            capacity=port.capacity, domain=port.domain,
        )
        for port in ports.values()
        if port.protocol is InterfaceProtocol.READY_VALID
    )
    groups: list[module_features.ModuleFeatureGroup] = []
    if all(endpoint in ready_valid_endpoints for endpoint in module.protocol_endpoints):
        groups.append(module_features.ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS)
    if all(
        endpoint.members and all(
            _aggregate_port_matches(endpoint, member, ports.get(f"{endpoint.name}__{member.name}"))
            for member in endpoint.members
        )
        for endpoint in module.aggregate_protocol_endpoints
    ):
        groups.append(module_features.ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS)
    return tuple(groups)


def _aggregate_port_matches(
    endpoint: ir_module.AggregateProtocolEndpoint,
    member: ir_module.ProtocolMember,
    port: ir_module.Port | None,
) -> bool:
    direction = (
        ir_module.PortDirection.OUTPUT if endpoint.role == member.source_role
        else ir_module.PortDirection.INPUT
    )
    return (
        port is not None
        and endpoint.role in (member.source_role, member.sink_role)
        and member.protocol in (InterfaceProtocol.WIRE, InterfaceProtocol.READY_VALID)
        and port.protocol is member.protocol
        and port.direction is direction
        and port.type == member.payload_type
        and port.domain == member.domain
    )


def validate_protocol_storage_composition(top: ir_module.Module) -> None:
    """Reject unsupported local combinations before choosing a Direct-SV plan."""

    kinds = module_features.ModuleFeatureKind
    seen: set[int] = set()
    pending = [top]
    while pending:
        module = pending.pop()
        if id(module) in seen:
            continue
        seen.add(id(module))
        pending.extend(reversed(module.children))
        storage = (*module.memories, *module.fifos, *module.roms)
        if not storage or not (
            module.arbiters or any(port.protocol is InterfaceProtocol.VC_CREDIT for port in module.ports)
        ):
            continue
        inventory = module_features.module_feature_inventory(module)
        for kind, protocol in (
            (kinds.ARBITER, "packet arbitration"),
            (kinds.VC_CREDIT, "VC credit"),
        ):
            if inventory.of_kind(kind):
                resource = storage[0]
                raise SystemVerilogEmissionError(
                    f"Direct-SV does not support {protocol} with storage "
                    f"'{resource.name}' in module '{module.name}'",
                    semantic_path=(module.name, resource.name),
                    code="ZL-BACKEND-SYSTEMVERILOG-PROTOCOL-STORAGE",
                    primary=resource.source_origin,
                    fixes=("place storage behind a separate wire or ready/valid boundary",),
                )


def account_emission_plan(
    module: ir_module.Module,
    plan: str,
    *extra: module_features.ModuleFeatureGroup,
) -> None:
    """Fail before rendering when a selected plan would omit typed entities."""

    try:
        inventory = module_features.module_feature_inventory(module)
        module_features.validate_feature_claims(
            inventory,
            module_features.claims_for_groups(module, plan, (*VALUE_GROUPS, *extra)),
            backend="direct_systemverilog",
            plan=plan,
        )
    except module_features.ModuleFeatureAccountingError as error:
        raise SystemVerilogEmissionError(
            str(error),
            semantic_path=(module.name,),
            code="ZL-BACKEND-SYSTEMVERILOG-FEATURE-ACCOUNTING",
        ) from error
