"""Compiler-owned lowering of typed external-module behavioral models.

The public external-module contract already names a pure, typed ZLang
function as its backend-independent semantic truth.  Native simulation must
therefore execute that function's meaning, never vendor HDL and never a host
callback.  This pass validates the semantic shell, expands the bounded
callable closure, and erases the external-module marker before the primitive
SimulationPlan boundary.
"""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.callables import CallableExpansionError, expand_callable_calls
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module, PortDirection


class ExternalModelSimulationLoweringError(ValueError):
    """A typed external model cannot be represented by primitive simulation."""


def lower_external_model(
    module: Module,
    *,
    max_depth: int = 64,
    max_nodes: int = 16_384,
) -> Module:
    """Return *module* with one exact external model expanded compiler-side.

    Ordinary modules are returned unchanged.  External modules are restricted
    by the language to one combinational scalar-wire result.  Revalidating that
    shape here makes restoration/mutation failures explicit instead of
    allowing malformed typed IR to cross the runtime boundary.
    """

    contract = module.external_contract
    if contract is None:
        return module
    if contract.logical_name != module.name:
        raise ExternalModelSimulationLoweringError(
            "external model logical name does not match its module"
        )
    if module.module_signature is None or contract.signature != module.module_signature:
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' changed its typed interface"
        )
    inputs = tuple(
        port for port in module.ports if port.direction is PortDirection.INPUT
    )
    outputs = tuple(
        port for port in module.ports if port.direction is PortDirection.OUTPUT
    )
    if (
        len(outputs) != 1
        or not inputs
        or any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports)
        or module.clock_domains
        or module.clock is not None
        or module.reset is not None
    ):
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' is not a combinational scalar-wire model"
        )
    effectful = (
        module.registers,
        module.next_assignments,
        module.rules,
        module.memories,
        module.roms,
        module.fifos,
        module.instances,
        module.elaborated_instances,
        module.children,
        module.connections,
        module.hierarchical_connections,
    )
    if any(effectful):
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' contains state, hierarchy, or effects"
        )
    definitions = (*module.functions, *module.callable_definitions)
    matches = tuple(
        definition
        for definition in definitions
        if definition.callee_identity == contract.model_callee_identity
    )
    if len(matches) != 1:
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' does not contain exactly one declared model"
        )
    model = matches[0]
    if len(module.assignments) != 1:
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' must contain one model assignment"
        )
    assignment = module.assignments[0]
    if assignment.target.name != outputs[0].name or not isinstance(
        assignment.expression, expr.Call
    ):
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' has a malformed model assignment"
        )
    call = assignment.expression
    if call.function != model.name or call.type != outputs[0].type:
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' model call changed signature"
        )
    expected_arguments = tuple((port.name, port.type) for port in inputs)
    actual_arguments = tuple(
        (argument.name, argument.type)
        if isinstance(argument, expr.InputRef)
        else (None, argument.type)
        for argument in call.arguments
    )
    if actual_arguments != expected_arguments:
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' model call does not match its inputs"
        )
    # Semantic construction uses an unqualified legacy call because the
    # external declaration itself selected the model.  Attach the already
    # verified stable identity before expansion so an unrelated overload can
    # never affect simulation.
    qualified = replace(call, callee_identity=contract.model_callee_identity)
    try:
        expanded = expand_callable_calls(
            qualified,
            definitions,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    except CallableExpansionError as error:
        raise ExternalModelSimulationLoweringError(str(error)) from error
    if expanded.type != outputs[0].type:
        raise ExternalModelSimulationLoweringError(
            f"external module '{module.name}' expanded model changed result type"
        )
    return replace(
        module,
        assignments=(replace(assignment, expression=expanded),),
        functions=(),
        callable_definitions=(),
        external_contract=None,
    )


__all__ = [
    "ExternalModelSimulationLoweringError",
    "lower_external_model",
]
