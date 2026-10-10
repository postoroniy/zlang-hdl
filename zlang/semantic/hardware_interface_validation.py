"""Cross-resource validation for a prepared module hardware interface."""

from __future__ import annotations

from collections.abc import Mapping

from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from .errors import SemanticError


def validate_hardware_interface(
    *,
    clock_domains: tuple[object, ...] | list[object],
    clock: str | None,
    reset: str | None,
    timing_names: set[str] | frozenset[str],
    symbols: Mapping[str, ir_module.Port],
    ports: tuple[ir_module.Port, ...] | list[ir_module.Port],
    csr_blocks: tuple[object, ...] | list[object],
    connections: tuple[ir_module.Connection, ...] | list[ir_module.Connection],
    arbiters: tuple[object, ...] | list[object],
    request_responses: tuple[ir_module.RequestResponseInterface, ...] | list[ir_module.RequestResponseInterface],
    request_response_symbols: Mapping[str, ir_module.RequestResponseInterface],
) -> frozenset[str]:
    """Validate requirements spanning independently prepared interface kinds."""

    if not clock_domains and any(
        port.protocol is ir_interfaces.InterfaceProtocol.CREDIT for port in ports
    ):
        raise SemanticError("credit interfaces require a module clock and reset")
    if not clock_domains and any(
        port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT for port in ports
    ):
        raise SemanticError(
            "virtual-channel credit interfaces require a module clock and reset"
        )
    if any(
        port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT for port in ports
    ) and (clock is None or reset is None):
        raise SemanticError(
            "virtual-channel credit interfaces require one module clock and reset"
        )
    if any(
        port.protocol is ir_interfaces.InterfaceProtocol.PACKET for port in ports
    ) and not arbiters:
        raise SemanticError(
            "packet interfaces currently require an explicit packet arbiter"
        )
    if not clock_domains and request_responses:
        raise SemanticError(
            "request/response interfaces require a module clock and reset"
        )
    if not clock_domains and csr_blocks:
        raise SemanticError("CSR blocks require a module clock and reset")
    if not clock_domains and any(
        connection.buffer_depth or connection.adapter is not None
        for connection in connections
    ):
        raise SemanticError(
            "buffered and adapted connections require a module clock and reset"
        )
    for timing_name in timing_names:
        if timing_name in symbols or timing_name in request_response_symbols:
            raise SemanticError(
                f"clock/reset name '{timing_name}' conflicts with a port"
            )
    csr_names = frozenset(block.name for block in csr_blocks)
    conflicts = csr_names & (symbols.keys() | request_response_symbols.keys())
    if conflicts:
        conflict = sorted(conflicts)[0]
        raise SemanticError(f"CSR block name '{conflict}' conflicts with a port")
    return csr_names


__all__ = ["validate_hardware_interface"]
