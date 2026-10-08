"""Fail-closed typed feature accounting for SystemVerilog plans."""

from __future__ import annotations

from zlang.backend import module_features
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
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
