# SPDX-License-Identifier: Apache-2.0
"""Type and ownership checking for simulation directives, outside hardware IR."""

from dataclasses import replace

from zlang.ir import types as ty
from zlang.ir import expressions as ex
from zlang.ast import nodes as ast
from zlang.ir.interfaces import InterfaceProtocol, ready_valid_field_name
from zlang.ir.module import Port, PortDirection
from zlang.ir.traversal import walk_expression
from zlang.simulation_io_model import IODrive, IOFinish, IORead, IOWrite, IOWhen
from zlang.simulation_io_targets import input_location, overlaps, dependency_names
from zlang.simulation_protocol_shared import (
    request_response_field_name,
    request_response_scalar_fields,
)
from zlang.simulation_plan_policy import MAX_PLAN_REGION_DEPTH, MAX_PLAN_EVENTS

from zlang.source import SourceOrigin

from .errors import SemanticError
from .expression_coercion import coerce_raw_target, walk_syntax_expressions
from .verification import VerificationSemanticAnalyzer


def analyze_simulation_blocks(context):
    """Use the authoritative expression analyzer and existing clock validation."""
    result = []
    seen = set()
    for block in context.module.simulation_blocks:
        try:
            if block.name in seen:
                raise SemanticError(f"duplicate simulation block '{block.name}'")
            seen.add(block.name)
            if block.clock not in {d.clock for d in context.clock_domains}:
                raise SemanticError(
                    f"simulation block '{block.name}' references unknown clock '{block.clock}'"
                )
            symbols = dict(context.contract_symbols)
            resets = []
            for domain in context.clock_domains:
                if domain.reset is not None:
                    name = f"zlang_simulation_reset_{domain.reset}"
                    while name in symbols:
                        name += "_"
                    symbols[domain.reset] = Port(
                        PortDirection.INPUT, name, ty.BitType()
                    )
                    if (domain.reset, name) not in resets:
                        resets.append((domain.reset, name))

            def check(value, expected=None):
                if any(
                    isinstance(
                        node,
                        (
                            ast.DelayExpr,
                            ast.PipelineExpr,
                            ast.ImplementExpr,
                            ast.ImplementationChoiceExpr,
                        ),
                    )
                    for node in walk_syntax_expressions(value)
                ):
                    raise SemanticError(
                        "simulation expressions cannot introduce pipelines, delays or implementation choices"
                    )
                typed = context.expression_context.expressions.check(
                    value, symbols, expected, context.expression_context
                )
                typed = coerce_raw_target(typed, expected)
                if expected is not None and typed.type != expected:
                    raise SemanticError(
                        f"simulation expression requires {expected}, got {typed.type}"
                    )
                VerificationSemanticAnalyzer._validate_domain(
                    context, typed, block.name, block.clock, "simulation expression"
                )
                return typed

            reads = []
            read_schema = None
            writes = {}
            owners = []
            command_count = 0

            def target(value, branch):
                root = value
                while isinstance(root, ast.FieldExpr):
                    root = root.expression
                public_names = set(context.ports) | set(context.request_responses)
                public_names.update(port.name for port in context.module.ports)
                if not isinstance(root, ast.NameExpr) or root.name not in public_names:
                    raise SemanticError(
                        "simulation read/drive target must name an environment-owned top input or interface field"
                    )
                typed = check(value)
                try:
                    location = input_location(
                        typed, context.ports, context.request_responses
                    )
                except ValueError as error:
                    raise SemanticError(str(error)) from error
                for previous, previous_branch in owners:
                    if overlaps(location, previous) and not (
                        branch is not None
                        and previous_branch is not None
                        and branch != previous_branch
                    ):
                        raise SemanticError(
                            f"overlapping simulation drivers for '{location.name}'"
                        )
                owners.append((location, branch))
                return typed

            def bindings(items, checker):
                names = [name for name, _ in items]
                if len(set(names)) != len(names):
                    raise SemanticError("duplicate simulation record field")
                return tuple((name, checker(value)) for name, value in items)

            def commands(items, guards=(), branch=None, eof=False, depth=0):
                nonlocal read_schema, command_count
                if depth > MAX_PLAN_REGION_DEPTH:
                    raise SemanticError(
                        "simulation control nesting exceeds native region depth limit"
                    )
                out = []
                for item in items:
                    command_count += 1
                    if command_count > MAX_PLAN_EVENTS:
                        raise SemanticError(
                            "simulation command count exceeds instrumentation event limit"
                        )
                    if isinstance(item, IOWhen):
                        guard = check(item.guard, ty.BitType())
                        out.append(
                            replace(
                                item,
                                guard=guard,
                                body=commands(
                                    item.body, (*guards, guard), branch, eof, depth + 1
                                ),
                            )
                        )
                    elif isinstance(item, IORead):
                        if eof:
                            raise SemanticError(
                                "read commands are not permitted in an EOF branch"
                            )
                        site = len(reads)
                        if reads and item.channel != reads[0]:
                            raise SemanticError(
                                "v1 simulation permits one input channel"
                            )
                        reads.append(item.channel)
                        fields = bindings(
                            item.bindings, lambda value: target(value, (site, True))
                        )
                        schema = tuple((name, value.type) for name, value in fields)
                        if read_schema is not None and read_schema != schema:
                            raise SemanticError("inconsistent simulation input schema")
                        read_schema = schema
                        out.append(
                            replace(
                                item,
                                bindings=fields,
                                eof=commands(
                                    item.eof, guards, (site, False), True, depth + 1
                                ),
                            )
                        )
                    elif isinstance(item, IODrive):
                        typed = target(item.target, branch)
                        out.append(
                            replace(
                                item, target=typed, value=check(item.value, typed.type)
                            )
                        )
                    elif isinstance(item, IOWrite):
                        fields = bindings(item.bindings, check)
                        schema = tuple((name, value.type) for name, value in fields)
                        if item.channel in writes and writes[item.channel] != schema:
                            raise SemanticError(
                                f"inconsistent simulation output schema '{item.channel}'"
                            )
                        writes[item.channel] = schema
                        out.append(replace(item, bindings=fields))
                    elif isinstance(item, IOFinish):
                        out.append(item)
                    else:
                        raise SemanticError("unsupported simulation command")
                return tuple(out)

            typed = commands(block.commands)
            if set(reads) & writes.keys():
                raise SemanticError(
                    "simulation input and output channels must be distinct"
                )
            result.append(replace(block, commands=typed, reset_bindings=tuple(resets)))
        except SemanticError as error:
            if error.primary is not None or block.origin is None:
                raise
            raise SemanticError(
                str(error),
                primary=SourceOrigin(
                    block.origin,
                    "simulation block",
                    context.source_unit,
                    context.source_digest,
                ),
            ) from error
    return tuple(result)


def validate_simulation_feedback(module):
    """Expand compiler-owned assignment dependencies; never simulate guard logic."""
    if not module.simulation_blocks:
        return
    assignments = {}
    assignments.update((value.name, value.expression) for value in module.locals)
    for item in module.assignments:
        name = (
            request_response_field_name(item.target.name, item.channel, item.signal)
            if item.channel is not None
            else item.target.name
            if item.target.protocol is InterfaceProtocol.WIRE
            else ready_valid_field_name(item.target.name, item.signal)
        )
        assignments[name] = item.expression

    def dependencies(expression):
        todo = [expression]
        visited = set()
        found = set()
        while todo:
            value = todo.pop()
            if id(value) in visited:
                continue
            visited.add(id(value))
            for node in walk_expression(value):
                for name in dependency_names(node):
                    found.add(name)
                    if name in assignments:
                        todo.append(assignments[name])
                if isinstance(node, ex.RequestResponseRef):
                    fields = request_response_scalar_fields(
                        request_responses[node.interface]
                    )
                    if node.signal.value == "transfer" or any(
                        not f.external
                        and f.channel is node.channel
                        and f.signal is node.signal
                        for f in fields
                    ):
                        # Admission can couple both channels. Until a finer typed
                        # dependency product exists, keep this check conservative.
                        found.update(
                            request_response_field_name(
                                node.interface, f.channel, f.signal
                            )
                            for f in fields
                            if f.external
                        )
        return found

    ports = {p.name: p for p in module.ports}
    request_responses = {p.name: p for p in module.request_responses}
    for block in module.simulation_blocks:
        driven = set()
        guarded = []

        def visit(items, guards=()):
            for item in items:
                if isinstance(item, IOWhen):
                    visit(item.body, (*guards, item.guard))
                elif isinstance(item, IORead):
                    driven.update(
                        input_location(value, ports, request_responses).name
                        for _, value in item.bindings
                    )
                    guarded.extend(guards)
                    visit(item.eof, guards)
                elif isinstance(item, IODrive):
                    driven.add(
                        input_location(item.target, ports, request_responses).name
                    )
                    guarded.extend(guards)
                    # Drive operands also use the immutable pre-drive snapshot.

        visit(block.commands)
        if any(dependencies(guard) & driven for guard in guarded):
            raise SemanticError(
                "simulation read/drive guard has feedback through a block-owned input",
                primary=SourceOrigin(
                    block.origin,
                    "simulation block",
                    module.source_identity,
                    module.source_hash,
                )
                if block.origin is not None
                else None,
            )
