"""SystemVerilog verification artifact backend."""

from zlang.backend.systemverilog.contracts import (
    ContractEmissionError,
    emit_contracts,
)
from zlang.backend.systemverilog.emitter import (
    SystemVerilogCapabilityReport,
    SystemVerilogEmissionError,
    capability_report,
    emit,
    emit_artifact,
    emit_artifact_with_source_map,
    emit_formal_artifact,
)
from zlang.backend.systemverilog.target import emit_target, emit_target_artifact
from zlang.backend.external import ExternalMappingError, ExternalPhysicalMapping
from zlang.backend.systemverilog.simulation_state import (
    SystemVerilogSimulationStateBundle,
    SystemVerilogStateLocator,
    build_systemverilog_simulation_state_bundle,
)

# Historical spelling retained as a thin compatibility alias.  The sole
# implementation remains the production direct-SystemVerilog emitter.
emit_experimental = emit

__all__ = [
    "ContractEmissionError",
    "SystemVerilogEmissionError",
    "SystemVerilogCapabilityReport",
    "capability_report",
    "emit_contracts",
    "emit",
    "emit_experimental",
    "emit_artifact",
    "emit_artifact_with_source_map",
    "emit_formal_artifact",
    "emit_target",
    "emit_target_artifact",
    "ExternalMappingError",
    "ExternalPhysicalMapping",
    "SystemVerilogSimulationStateBundle",
    "SystemVerilogStateLocator",
    "build_systemverilog_simulation_state_bundle",
]
