"""Public target-catalog and architecture-mapping API."""

from zlang.target_catalog import (
    ArchitectureSelectionMode,
    TargetArchitectureError,
    load_architecture,
    load_architecture_templates,
    load_target,
    validate_clock_requirement,
    validate_inventory,
    validate_memory_configuration,
    validate_pipeline_configuration,
)
from zlang.target_mapping_dsp import (
    map_auto_multiply_add_configuration,
    map_auto_signed_product_configuration,
)
from zlang.target_mapping_fir import (
    map_auto_symmetric_configuration,
    map_manual_architecture,
)
from zlang.target_mapping_selection import (
    generic_implementation_graph,
    select_implementation_graph,
)

__all__ = [
    "ArchitectureSelectionMode",
    "TargetArchitectureError",
    "generic_implementation_graph",
    "load_architecture",
    "load_architecture_templates",
    "load_target",
    "map_auto_multiply_add_configuration",
    "map_auto_signed_product_configuration",
    "map_auto_symmetric_configuration",
    "map_manual_architecture",
    "select_implementation_graph",
    "validate_clock_requirement",
    "validate_inventory",
    "validate_memory_configuration",
    "validate_pipeline_configuration",
]
