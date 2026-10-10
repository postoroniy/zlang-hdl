"""Typed external-module model resolution and signature validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from zlang.ast import nodes as ast
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from .errors import SemanticError


def resolve_external_model_function(
    module: ast.Module,
    source_ports: Sequence[ir_module.Port],
    functions: Sequence[ir_module.Function],
    generic_functions: Mapping[str, ast.FunctionDecl],
) -> ir_module.Function | None:
    """Return the exact typed model function for one external module."""

    if module.external_model is None:
        return None
    aggregate_port = next(
        (
            port
            for port in source_ports
            if isinstance(
                port.type,
                (ir_types.StructType, ir_types.TupleType, ir_types.VecType),
            )
        ),
        None,
    )
    if aggregate_port is not None:
        raise SemanticError(
            f"external module '{module.name}' port '{aggregate_port.name}' "
            "must be a scalar wire in this first slice",
            code="ZL-EXTERN-UNSUPPORTED",
        )
    model = next(
        (item for item in functions if item.name == module.external_model),
        None,
    )
    if model is None:
        detail = (
            "must be non-generic"
            if module.external_model in generic_functions
            else "does not name an existing function"
        )
        raise SemanticError(
            f"external module '{module.name}' model '{module.external_model}' "
            f"{detail}",
            code="ZL-EXTERN-MODEL",
        )
    inputs = tuple(
        port for port in source_ports
        if port.direction is ir_module.PortDirection.INPUT
    )
    outputs = tuple(
        port for port in source_ports
        if port.direction is ir_module.PortDirection.OUTPUT
    )
    expected_parameters = tuple((port.name, port.type) for port in inputs)
    actual_parameters = tuple(
        (parameter.name, parameter.type) for parameter in model.parameters
    )
    if actual_parameters != expected_parameters:
        raise SemanticError(
            f"external module '{module.name}' model parameters must exactly "
            f"match inputs {expected_parameters}, got {actual_parameters}",
            code="ZL-EXTERN-MODEL",
        )
    if model.return_type != outputs[0].type:
        raise SemanticError(
            f"external module '{module.name}' model returns {model.return_type}, "
            f"expected {outputs[0].type}",
            code="ZL-EXTERN-MODEL",
        )
    return model


__all__ = ["resolve_external_model_function"]
