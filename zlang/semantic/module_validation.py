# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Shared fail-closed graph validation for typed module connectivity."""

from __future__ import annotations

import ast as pyast
from collections.abc import Callable, Iterable, Mapping
from typing import TypeVar

from zlang.ast import nodes as ast

from .errors import SemanticError

_Node = TypeVar("_Node")


def validate_value_parameter_shadowing(module: ast.Module) -> None:
    parameters = {
        item.name: item.kind
        for item in module.parameters
        if item.kind in {"value", "constant", "callable"}
    }
    if not parameters:
        return
    declarations = [
        *(item.name for item in module.ports),
        *(item.name for item in module.registers),
        *(item.name for item in module.fifos),
        *(item.name for item in module.memories),
        *(item.name for item in module.roms),
        *(item.name for item in module.instances),
    ]
    port_names = {item.name for item in module.ports}
    declarations.extend(
        item.target
        for item in module.assignments
        if "." not in item.target and item.target not in port_names
    )
    conflict = next((name for name in declarations if name in parameters), None)
    if conflict is not None:
        kind = parameters[conflict]
        label = (
            "module value parameter"
            if kind == "value"
            else f"compile-time module {kind} parameter"
        )
        raise SemanticError(
            f"declaration '{conflict}' conflicts with {label} '{conflict}'"
        )


def validate_compile_time_parameter_declarations(module: ast.Module) -> None:
    """Validate the bounded typed/callable parameter surface once."""

    def validate(
        owner: str,
        parameters: tuple[ast.ModuleParameter, ...],
    ) -> None:
        names = tuple(item.name for item in parameters)
        if len(names) != len(set(names)):
            duplicate = next(name for name in names if names.count(name) > 1)
            raise SemanticError(
                f"duplicate compile-time parameter '{duplicate}' in {owner}"
            )
        shaped_phase = True
        for index, parameter in enumerate(parameters):
            if parameter.kind == "value" and isinstance(parameter.default, str):
                try:
                    default_tree = pyast.parse(parameter.default, mode="eval")
                except SyntaxError as error:
                    raise SemanticError(
                        f"invalid default for value parameter '{parameter.name}' "
                        f"in {owner}"
                    ) from error
                intrinsic_names = {
                    node.func.id
                    for node in pyast.walk(default_tree)
                    if isinstance(node, pyast.Call)
                    and isinstance(node.func, pyast.Name)
                }
                references = {
                    node.id
                    for node in pyast.walk(default_tree)
                    if isinstance(node, pyast.Name)
                    and node.id not in intrinsic_names
                }
                invalid = next(
                    (name for name in names[index:] if name in references),
                    None,
                )
                if invalid is not None:
                    raise SemanticError(
                        f"value parameter '{parameter.name}' in {owner} default "
                        f"references later parameter '{invalid}'; defaults may "
                        "reference only earlier parameters",
                        code="ZL-SEMANTIC-PARAMETER-DEFAULT",
                    )
            if parameter.kind in {"constant", "callable"}:
                shaped_phase = False
                if parameter.default is not None:
                    raise SemanticError(
                        f"compile-time {parameter.kind} parameter "
                        f"'{parameter.name}' in {owner} cannot have a default"
                    )
                if parameter.kind == "constant" and parameter.type_name is None:
                    raise SemanticError(
                        f"compile-time constant parameter '{parameter.name}' in "
                        f"{owner} requires an exact type"
                    )
                if (
                    parameter.kind == "callable"
                    and parameter.callable_return_type is None
                ):
                    raise SemanticError(
                        f"compile-time callable parameter '{parameter.name}' in "
                        f"{owner} requires an exact return type"
                    )
            elif not shaped_phase:
                raise SemanticError(
                    f"type/integer parameter '{parameter.name}' in {owner} must "
                    "precede typed constant and callable parameters"
                )

    validate(f"module '{module.name}'", module.parameters)
    for declaration in module.submodules:
        validate(f"module '{declaration.name}'", declaration.parameters)
    for declaration in module.functions:
        validate(f"function '{declaration.name}'", declaration.generic_parameters)
    for declaration in module.operators:
        validate(
            f"operator '{declaration.operator}'", declaration.generic_parameters
        )
    for kind, declarations in (
        ("struct", module.structs),
        ("protocol", module.protocols),
        ("module interface", module.module_interfaces),
    ):
        for declaration in declarations:
            unsupported = next(
                (
                    item
                    for item in declaration.parameters
                    if item.kind in {"constant", "callable"}
                ),
                None,
            )
            if unsupported is not None:
                raise SemanticError(
                    f"{kind} '{declaration.name}' does not accept compile-time "
                    f"{unsupported.kind} parameters in this first slice"
                )


def reject_dependency_cycles(
    graph: Mapping[_Node, Iterable[_Node]],
    *,
    render_node: Callable[[_Node], str],
    description: Callable[[tuple[_Node, ...]], str] | str,
    stable_sort: bool = True,
) -> None:
    """Reject a deterministic dependency cycle without changing graph meaning."""

    visited: set[_Node] = set()
    active: list[_Node] = []

    def visit(node: _Node) -> None:
        if node in active:
            start = active.index(node)
            cycle_nodes = (*active[start:], node)
            label = (
                description(cycle_nodes)
                if callable(description)
                else description
            )
            cycle = " -> ".join(render_node(item) for item in cycle_nodes)
            raise SemanticError(f"combinational {label} dependency cycle: {cycle}")
        if node in visited or node not in graph:
            return
        active.append(node)
        dependencies = graph[node]
        for dependency in sorted(dependencies) if stable_sort else dependencies:
            visit(dependency)
        active.pop()
        visited.add(node)

    nodes = sorted(graph) if stable_sort else graph
    for node in nodes:
        visit(node)
