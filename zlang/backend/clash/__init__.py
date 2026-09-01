"""Clash backend."""

from zlang.backend.clash.emitter import (
    ClashEmissionError,
    emit,
    emit_artifact,
    emit_artifact_with_source_map,
    emit_formal_artifact,
)
from zlang.backend.clash.formal_registers import (
    finalize_formal_verilog_artifact,
    run_recursive_register_formal,
    validate_register_formal_artifact,
)
from zlang.backend.clash.public_wrapper import (
    ClashPublicTopWrapper,
    ClashPublicWrapperError,
    bind_artifact_to_public_wrapper,
)

__all__ = [
    "ClashEmissionError", "emit", "emit_artifact", "emit_artifact_with_source_map",
    "emit_formal_artifact",
    "finalize_formal_verilog_artifact",
    "validate_register_formal_artifact", "run_recursive_register_formal",
    "ClashPublicTopWrapper", "ClashPublicWrapperError",
    "bind_artifact_to_public_wrapper",
]
