"""Read-only registry of compatibility-sensitive compiler schemas.

Each subsystem continues to own and validate its serialized representation.
This module only provides one deterministic inventory for cache/build recipes;
it deliberately does not decode any of those representations.
"""

from __future__ import annotations

from dataclasses import dataclass

from zlang.common import canonical_identity


COMPILER_SCHEMA_REGISTRY_SCHEMA = "zlang-compiler-schema-registry-v1"


@dataclass(frozen=True)
class CompilerSchema:
    owner: str
    schema: str | int


def compiler_schema_registry() -> tuple[CompilerSchema, ...]:
    """Return the compatibility schemas that cross compilation phases."""
    # Imports stay local so querying one subsystem never creates a new import
    # dependency between the individual schema owners.
    from zlang.backend.expression_materialization import (
        DIRECT_SV_DAG_SCHEMA,
        FUNCTIONAL_REGION_EMISSION_SCHEMA,
    )
    from zlang.backend.naming import RTL_NAMING_SCHEMA
    from zlang.ir.expression_arena import SEMANTIC_EXPRESSION_ARENA_SCHEMA
    from zlang.ir.normalization import NORMALIZATION_SCHEMA
    from zlang.ir.packing import PACKING_LAYOUT_SCHEMA
    from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
    from zlang.simulation_plan import SIMULATION_PLAN_SCHEMA
    from zlang.simulation_state import SIMULATION_STATE_SCHEMA
    from zlang.tooling import SYMBOL_CACHE_SCHEMA, TOOLING_API_SCHEMA

    values = (
        CompilerSchema("backend.direct_sv_dag", DIRECT_SV_DAG_SCHEMA),
        CompilerSchema(
            "backend.functional_region_emission",
            FUNCTIONAL_REGION_EMISSION_SCHEMA,
        ),
        CompilerSchema("backend.rtl_naming", RTL_NAMING_SCHEMA),
        CompilerSchema("ir.canonical", CANONICAL_IR_IDENTITY_SCHEMA),
        CompilerSchema("ir.normalization", NORMALIZATION_SCHEMA),
        CompilerSchema("ir.packing", PACKING_LAYOUT_SCHEMA),
        CompilerSchema(
            "semantic.expression_arena",
            SEMANTIC_EXPRESSION_ARENA_SCHEMA,
        ),
        CompilerSchema("simulation.plan", SIMULATION_PLAN_SCHEMA),
        CompilerSchema("simulation.state", SIMULATION_STATE_SCHEMA),
        CompilerSchema("tooling.api", TOOLING_API_SCHEMA),
        CompilerSchema("tooling.symbol_cache", SYMBOL_CACHE_SCHEMA),
    )
    return tuple(sorted(values, key=lambda value: value.owner))


def compiler_compatibility_identity() -> str:
    """Identify the complete cross-phase compatibility contract."""
    payload = tuple(
        {"owner": item.owner, "schema": item.schema}
        for item in compiler_schema_registry()
    )
    return canonical_identity(COMPILER_SCHEMA_REGISTRY_SCHEMA, payload)


__all__ = [
    "COMPILER_SCHEMA_REGISTRY_SCHEMA",
    "CompilerSchema",
    "compiler_compatibility_identity",
    "compiler_schema_registry",
]
