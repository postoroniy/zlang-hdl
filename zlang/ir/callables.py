"""Stable identities and bounded expansion for typed callable definitions.

Generic semantic elaboration may publish one monomorphic ``Function`` body per
specialization and retain calls to that body instead of cloning it at every use.
This module deliberately depends only on typed IR objects; it contains no AST or
backend behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from typing import Iterable

from zlang.common import stable_digest
from zlang.common.identity_memo import IdentityMemo
from zlang.ir import expressions as expr
from zlang.ir.functional_regions import CompileTimeBinderRef
from zlang.ir.types import HardwareType


CALLABLE_IDENTITY_SCHEMA = "zlang-callable-definition-v1"
DEFAULT_CALLABLE_EXPANSION_DEPTH = 64
DEFAULT_CALLABLE_EXPANSION_NODES = 65_536


class CallableKind(str, Enum):
    FUNCTION = "function"
    OPERATOR = "operator"


def source_function_metadata(
    name: str,
    source_identity: str,
) -> "CallableMetadata":
    """Build canonical metadata for one non-generic source function."""

    if not source_identity:
        raise ValueError("source function identity must not be empty")
    return CallableMetadata(
        CallableKind.FUNCTION,
        name,
        f"{source_identity}:{name}",
    )


@dataclass(frozen=True)
class CallableMetadata:
    """Semantic provenance for one monomorphic callable definition.

    ``arguments`` uses the existing rendered generic-argument spelling.  A
    concrete specialization may retain its pre-existing specialization digest;
    otherwise the callable identity is derived from this metadata and the exact
    typed signature.
    """

    kind: CallableKind
    source_name: str
    declaration_identity: str
    specialization_identity: str | None = None
    arguments: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CallableKind):
            try:
                object.__setattr__(self, "kind", CallableKind(self.kind))
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid callable kind {self.kind!r}") from error
        if not self.source_name:
            raise ValueError("callable source name must not be empty")
        if not self.declaration_identity:
            raise ValueError("callable declaration identity must not be empty")
        if self.specialization_identity == "":
            raise ValueError("callable specialization identity must not be empty")
        names = tuple(name for name, _ in self.arguments)
        if any(not name for name in names):
            raise ValueError("callable generic argument name must not be empty")
        if len(names) != len(set(names)):
            raise ValueError("callable generic argument names must be unique")
        if any(not isinstance(value, str) for _, value in self.arguments):
            raise ValueError("callable generic argument values must be rendered strings")


def stable_callee_identity(
    parameters: tuple[object, ...],
    return_type: HardwareType,
    metadata: CallableMetadata,
) -> str:
    """Return the stable semantic identity of one exact callable signature."""

    if metadata.specialization_identity is not None:
        return metadata.specialization_identity
    provenance = {
        "kind": metadata.kind.value,
        "source_name": metadata.source_name,
        "declaration_identity": metadata.declaration_identity,
        "arguments": metadata.arguments,
    }
    signature = tuple(
        (
            str(getattr(parameter, "name", "")),
            _identity_value(getattr(parameter, "type", None)),
        )
        for parameter in parameters
    )
    return stable_digest(
        {
            "schema": CALLABLE_IDENTITY_SCHEMA,
            "provenance": provenance,
            "parameters": signature,
            "return_type": _identity_value(return_type),
        }
    )


def _identity_value(value: object) -> object:
    if isinstance(value, Enum):
        return {
            "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value.value,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "$type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": [
                [item.name, _identity_value(getattr(value, item.name))]
                for item in fields(value)
                if item.name not in {"origin", "source_origin"}
            ],
        }
    if isinstance(value, tuple):
        return [_identity_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _callable_definition_value(value: object) -> str:
    """Fingerprint one callable definition modulo bound local identities.

    A monomorphic source callable is typed in each physical module context that
    publishes it.  Compact functional regions inside that body consequently
    receive context-local binder/capture identities even though the callable's
    exact signature, body, and semantic identity are unchanged.  Those names
    are alpha-bound implementation details: comparing them literally made a
    hierarchy-wide backend reject one valid specialization as several
    conflicting definitions.

    Normalize only identities introduced by a bounded functional region.  All
    executable structure, domains, lookup tables, types, callable edges, and
    metadata remain part of the comparison, so two genuinely different bodies
    carrying the same callee identity are still rejected.
    """

    binder_names: dict[str, str] = {}
    capture_names: dict[str, str] = {}
    fingerprints: dict[int, str] = {}

    def bound_name(names: dict[str, str], identity: str, prefix: str) -> str:
        current = names.get(identity)
        if current is None:
            current = f"{prefix}{len(names)}"
            names[identity] = current
        return current

    def fingerprint(current: object) -> object:
        structured = (
            isinstance(current, (tuple, list, dict))
            or (is_dataclass(current) and not isinstance(current, type))
        )
        if structured:
            cached = fingerprints.get(id(current))
            if cached is not None:
                return cached
        if isinstance(current, CompileTimeBinderRef):
            payload = {
                "$type": (
                    f"{type(current).__module__}.{type(current).__qualname__}"
                ),
                "fields": [
                    ["identity", bound_name(binder_names, current.identity, "b")],
                    ["display_name", current.display_name],
                    ["start", current.start],
                    ["stop", current.stop],
                ],
            }
        elif isinstance(current, expr.FunctionalCaptureRef):
            payload = {
                "$type": (
                    f"{type(current).__module__}.{type(current).__qualname__}"
                ),
                "fields": [
                    ["identity", bound_name(capture_names, current.identity, "c")],
                    ["display_name", current.display_name],
                    ["type", fingerprint(current.type)],
                ],
            }
        elif isinstance(current, Enum):
            return {
                "$enum": f"{type(current).__module__}.{type(current).__qualname__}",
                "value": current.value,
            }
        elif is_dataclass(current) and not isinstance(current, type):
            payload = {
                "$type": f"{type(current).__module__}.{type(current).__qualname__}",
                "fields": [
                    [item.name, fingerprint(getattr(current, item.name))]
                    for item in fields(current)
                    if item.name not in {"origin", "source_origin"}
                ],
            }
        elif isinstance(current, tuple):
            payload = ["tuple", [fingerprint(item) for item in current]]
        elif isinstance(current, list):
            payload = ["list", [fingerprint(item) for item in current]]
        elif isinstance(current, dict):
            payload = {
                str(key): fingerprint(item)
                for key, item in sorted(current.items(), key=lambda pair: str(pair[0]))
            }
        elif current is None or isinstance(current, (str, int, bool)):
            return current
        else:
            return str(current)
        result = stable_digest(payload)
        fingerprints[id(current)] = result
        return result

    return str(fingerprint(value))


def _callable_definitions_equivalent(
    left: object,
    right: object,
) -> bool:
    """Compare exact definitions modulo local binder and capture identities."""

    return _callable_definition_value(left) == _callable_definition_value(right)


class CallableExpansionError(ValueError):
    """A typed call graph cannot be expanded within the requested bounds."""


class CallableReachabilityError(ValueError):
    """Executable typed IR references an invalid callable graph."""


@dataclass(frozen=True)
class CallableUse:
    """One identity-bearing call edge retained by executable typed IR."""

    function: str
    callee_identity: str


def callable_uses(
    value: object,
    *,
    deduplicate: bool = True,
) -> tuple[CallableUse, ...]:
    """Return every typed callable edge reachable inside ``value``.

    Exact nominal reductions deliberately retain their combine operations in
    :class:`~zlang.ir.functional_regions.ExactReductionPlan` rather than as
    ordinary ``Call`` expression nodes.  Treat those plan operations as call
    edges too, so a backend can keep the nominal reduction compact without
    dropping its monomorphic helper declaration.

    Diagnostic-only origins are excluded.  Definition containers are excluded
    by :func:`reachable_module_callables`, which passes only executable module
    fields to this generic value walker.
    """

    result: list[CallableUse] = []
    visited: dict[int, object] = {}

    def visit(current: object) -> None:
        if deduplicate and (isinstance(current, (tuple, list, dict)) or (
            is_dataclass(current) and not isinstance(current, type)
        )):
            previous = visited.get(id(current))
            if previous is current:
                return
            visited[id(current)] = current
        if isinstance(current, expr.Call):
            result.append(CallableUse(current.function, current.callee_identity))
        if isinstance(current, expr.Reduce) and current.plan is not None:
            for level in current.plan.levels:
                for operation in level.operations:
                    if operation.callee_identity is None:
                        continue
                    if operation.function is None:
                        raise CallableReachabilityError(
                            "exact reduction callable identity has no function name"
                        )
                    result.append(
                        CallableUse(
                            operation.function,
                            operation.callee_identity,
                        )
                    )
        if isinstance(current, (tuple, list)):
            for item in current:
                visit(item)
            return
        if isinstance(current, dict):
            for item in current.values():
                visit(item)
            return
        if is_dataclass(current) and not isinstance(current, type):
            for item in fields(current):
                if item.name in {"origin", "source_origin"}:
                    continue
                visit(getattr(current, item.name))

    visit(value)
    return tuple(result)


def reachable_callable_definitions(
    definitions: Iterable[object],
    roots: Iterable[object],
    *,
    _definition_values: dict[int, tuple[object, str]] | None = None,
    _uses: dict[int, tuple[object, tuple[CallableUse, ...]]] | None = None,
) -> tuple[object, ...]:
    """Select the deterministic transitive callable closure of ``roots``.

    Definitions use the same structural interface as
    :func:`expand_callable_calls`.  The returned order is a stable topological
    order: dependencies precede their users, with semantic callable identity
    breaking otherwise independent ties.  This is suitable for backend helper
    declaration emission and is independent of source/import discovery order.

    Only reachable name conflicts, unknown calls, and recursion are rejected.
    Consequently an imported but unused helper cannot alter generated RTL or
    make an otherwise valid selected top fail in a backend.
    """

    definitions_by_identity: dict[str, list[object]] = {}
    for definition in definitions:
        identity = str(getattr(definition, "callee_identity", ""))
        name = str(getattr(definition, "name", ""))
        if not identity or not name:
            raise CallableReachabilityError(
                "callable definition requires a name and stable callee identity"
            )
        definitions_by_identity.setdefault(identity, []).append(definition)

    grouped_definitions = {
        identity: tuple(items)
        for identity, items in definitions_by_identity.items()
    }
    definition_values = IdentityMemo(_definition_values)
    uses_by_object = IdentityMemo(_uses)

    by_identity: dict[str, object] = {}
    for identity in sorted(grouped_definitions):
        candidates = grouped_definitions[identity]
        # A singleton needs no structural ordering key, but still needs the
        # full validation below (including nested callee-name consistency).
        representative = (
            candidates[0] if len(candidates) == 1
            else min(
                candidates,
                key=lambda item: definition_values.get_or_compute(
                    item, _callable_definition_value
                ),
            )
        )
        representative_value = (
            definition_values.get_or_compute(
                representative, _callable_definition_value
            )
            if len(candidates) > 1
            else None
        )
        for definition in candidates:
            inconsistent_reference = any(
                targets
                and all(
                    getattr(target, "name", None) != use.function
                    for target in targets
                )
                for use in uses_by_object.get_or_compute(
                    getattr(definition, "body"), callable_uses
                )
                if (targets := grouped_definitions.get(use.callee_identity, ()))
            )
            if (
                getattr(definition, "name", None)
                != getattr(representative, "name", None)
                or inconsistent_reference
                or (
                    representative_value is not None
                    and representative_value
                    != definition_values.get_or_compute(
                        definition, _callable_definition_value
                    )
                )
            ):
                raise CallableReachabilityError(
                    f"callable identity '{identity}' has conflicting definitions"
                )
        by_identity[identity] = representative

    def use_key(use: CallableUse) -> tuple[str, str]:
        return (use.callee_identity, use.function)

    def resolve(use: CallableUse) -> object:
        definition = by_identity.get(use.callee_identity)
        if definition is None:
            raise CallableReachabilityError(
                f"call '{use.function}' references unknown typed callable "
                f"'{use.callee_identity}'"
            )
        if getattr(definition, "name") != use.function:
            raise CallableReachabilityError(
                f"call name '{use.function}' does not match typed callable "
                f"'{use.callee_identity}'"
            )
        return definition

    state: dict[str, int] = {}
    stack: list[str] = []
    ordered: list[object] = []

    def visit(definition: object) -> None:
        identity = str(getattr(definition, "callee_identity"))
        status = state.get(identity, 0)
        if status == 2:
            return
        if status == 1:
            start = stack.index(identity)
            cycle = " -> ".join((*stack[start:], identity))
            raise CallableReachabilityError(
                f"recursive typed callable cycle: {cycle}"
            )
        state[identity] = 1
        stack.append(identity)
        dependencies = sorted(
            uses_by_object.get_or_compute(
                getattr(definition, "body"), callable_uses
            ),
            key=use_key,
        )
        for dependency in dependencies:
            visit(resolve(dependency))
        stack.pop()
        state[identity] = 2
        ordered.append(definition)

    root_uses = sorted(
        (
            use
            for root in roots
            for use in uses_by_object.get_or_compute(root, callable_uses)
        ),
        key=use_key,
    )
    for use in root_uses:
        visit(resolve(use))

    reachable_names: dict[str, object] = {}
    for definition in ordered:
        name = str(getattr(definition, "name"))
        previous = reachable_names.get(name)
        if previous is not None and previous is not definition:
            # Backends emit one declaration per returned definition.  Two
            # distinct identities cannot therefore share one emitted helper
            # name, even when their bodies happen to be equivalent.
            raise CallableReachabilityError(
                f"typed function name '{name}' has conflicting reachable definitions"
            )
        reachable_names[name] = definition
    return tuple(ordered)


def reachable_module_callables(
    module: object,
    *,
    include_hierarchy: bool = False,
    _definition_values: dict[int, tuple[object, str]] | None = None,
    _uses: dict[int, tuple[object, tuple[CallableUse, ...]]] | None = None,
) -> tuple[object, ...]:
    """Return callables reachable from one typed module or module hierarchy.

    The function intentionally uses the structural ``Module`` interface to
    keep this backend-independent utility free of an import cycle with
    :mod:`zlang.ir.module`.
    """

    modules: list[object] = []

    def add(current: object) -> None:
        modules.append(current)
        if include_hierarchy:
            for child in getattr(current, "children", ()):
                add(child)

    add(module)
    definitions = tuple(
        definition
        for current in modules
        for definition in (
            *getattr(current, "functions", ()),
            *getattr(current, "callable_definitions", ()),
        )
    )
    roots: list[object] = []
    for current in modules:
        if not is_dataclass(current) or isinstance(current, type):
            raise CallableReachabilityError(
                "callable reachability requires a typed dataclass module"
            )
        for item in fields(current):
            if item.name in {"functions", "callable_definitions", "children"}:
                continue
            roots.append(getattr(current, item.name))
    return reachable_callable_definitions(
        definitions,
        roots,
        _definition_values=_definition_values,
        _uses=_uses,
    )


def expand_callable_calls(
    expression: expr.Expression,
    definitions: Iterable[object],
    *,
    max_depth: int = DEFAULT_CALLABLE_EXPANSION_DEPTH,
    max_nodes: int = DEFAULT_CALLABLE_EXPANSION_NODES,
) -> expr.Expression:
    """Expand typed calls without permitting cycles or unbounded cloning.

    Definitions use the structural ``Function`` interface (``name``,
    ``parameters``, ``return_type``, ``body``, and ``callee_identity``).  This
    keeps the utility usable by backend-independent consumers without creating
    an import cycle with :mod:`zlang.ir.module`.
    """

    if max_depth < 1:
        raise ValueError("callable expansion max_depth must be positive")
    if max_nodes < 1:
        raise ValueError("callable expansion max_nodes must be positive")
    ordered = tuple(definitions)
    by_identity: dict[str, object] = {}
    for definition in ordered:
        identity = str(getattr(definition, "callee_identity", ""))
        name = str(getattr(definition, "name", ""))
        if not identity or not name:
            raise CallableExpansionError(
                "callable definition requires a name and stable callee identity"
            )
        previous = by_identity.get(identity)
        if previous is not None:
            if not _callable_definitions_equivalent(previous, definition):
                raise CallableExpansionError(
                    f"duplicate callable identity '{identity}'"
                )
            continue
        by_identity[identity] = definition

    nodes = 0

    def account(amount: int = 1) -> None:
        nonlocal nodes
        nodes += amount
        if nodes > max_nodes:
            raise CallableExpansionError(
                f"callable expansion exceeds {max_nodes} expression nodes"
            )

    def resolve(call: expr.Call) -> object:
        if call.callee_identity is None:
            raise CallableExpansionError(
                f"call '{call.function}' has no typed callable identity"
            )
        definition = by_identity.get(call.callee_identity)
        if definition is None:
            raise CallableExpansionError(
                f"unknown callable identity '{call.callee_identity}'"
            )
        if getattr(definition, "name") != call.function:
            raise CallableExpansionError(
                f"call name '{call.function}' does not match callable identity "
                f"'{call.callee_identity}'"
            )
        return definition

    mapped: dict[
        tuple[int, tuple[str, ...]], tuple[object, object, int]
    ] = {}
    visited: dict[
        tuple[int, tuple[str, ...]], tuple[expr.Expression, expr.Expression, int]
    ] = {}

    def map_value(value: object, stack: tuple[str, ...]) -> object:
        if isinstance(value, expr.Expression):
            return visit(value, stack)
        if not isinstance(value, tuple) and not (
            is_dataclass(value) and not isinstance(value, type)
        ):
            return value
        key = (id(value), stack)
        cached = mapped.get(key)
        if cached is not None and cached[0] is value:
            account(cached[2])
            return cached[1]
        before = nodes
        if isinstance(value, tuple):
            result = tuple(map_value(item, stack) for item in value)
        else:
            updates = {
                item.name: map_value(getattr(value, item.name), stack)
                for item in fields(value)
                if item.init and item.name not in {"type", "origin"}
            }
            try:
                result = replace(value, **updates)
            except (TypeError, ValueError):
                result = value
        mapped[key] = (value, result, nodes - before)
        return result

    def substitute(value: object, bindings: dict[str, expr.Expression]) -> object:
        substituted: dict[int, tuple[object, object]] = {}

        def walk(current: object) -> object:
            if isinstance(current, expr.ParameterRef) and current.name in bindings:
                return bindings[current.name]
            if not isinstance(current, (expr.Expression, tuple)) and not (
                is_dataclass(current) and not isinstance(current, type)
            ):
                return current
            cached = substituted.get(id(current))
            if cached is not None and cached[0] is current:
                return cached[1]
            if isinstance(current, tuple):
                result = tuple(walk(item) for item in current)
            else:
                updates = {
                    item.name: walk(getattr(current, item.name))
                    for item in fields(current)
                    if item.init and item.name not in {"type", "origin"}
                }
                try:
                    result = replace(current, **updates) if updates else current
                except (TypeError, ValueError):
                    result = current
            substituted[id(current)] = (current, result)
            return result

        return walk(value)

    def visit(value: expr.Expression, stack: tuple[str, ...]) -> expr.Expression:
        key = (id(value), stack)
        cached = visited.get(key)
        if cached is not None and cached[0] is value:
            account(cached[2])
            return cached[1]
        before = nodes
        account()
        if isinstance(value, expr.Reduce):
            # ``Reduce.expanded`` is a frozen exact-overload implementation,
            # not another high-level operand.  Consumers such as exact reduction planning and the
            # e-graph must continue to see the nominal reduction boundary and
            # must not accidentally inline or reinterpret that operator tree.
            # Parameter substitution has already crossed this field when the
            # Reduce itself came from a callable body, so leaving it opaque here
            # cannot strand formal parameters.
            collection = map_value(value.collection, stack)
            result = replace(value, collection=collection)
        elif not isinstance(value, expr.Call):
            updates = {
                item.name: map_value(getattr(value, item.name), stack)
                for item in fields(value)
                if item.init and item.name not in {"type", "origin"}
            }
            result = replace(value, **updates) if updates else value
        else:
            definition = resolve(value)
            identity = str(getattr(definition, "callee_identity"))
            if identity in stack:
                cycle = " -> ".join((*stack, identity))
                raise CallableExpansionError(f"callable expansion cycle: {cycle}")
            if len(stack) >= max_depth:
                raise CallableExpansionError(
                    f"callable expansion exceeds depth {max_depth} at '{value.function}'"
                )
            arguments = tuple(visit(argument, stack) for argument in value.arguments)
            parameters = tuple(getattr(definition, "parameters"))
            if len(arguments) != len(parameters):
                raise CallableExpansionError(
                    f"callable '{value.function}' expects {len(parameters)} arguments, "
                    f"got {len(arguments)}"
                )
            for parameter, argument in zip(parameters, arguments, strict=True):
                if getattr(parameter, "type") != argument.type:
                    raise CallableExpansionError(
                        f"callable argument '{getattr(parameter, 'name')}' has type "
                        f"{argument.type}, expected {getattr(parameter, 'type')}"
                    )
            if getattr(definition, "return_type") != value.type:
                raise CallableExpansionError(
                    f"callable '{value.function}' call type {value.type} does not match "
                    f"definition return type {getattr(definition, 'return_type')}"
                )
            bindings = {
                str(getattr(parameter, "name")): argument
                for parameter, argument in zip(parameters, arguments, strict=True)
            }
            expanded = substitute(getattr(definition, "body"), bindings)
            assert isinstance(expanded, expr.Expression)
            expanded = visit(expanded, (*stack, identity))
            result = (
                replace(expanded, origin=value.origin)
                if value.origin is not None else expanded
            )
        visited[key] = (value, result, nodes - before)
        return result

    return visit(expression, ())


__all__ = [
    "CALLABLE_IDENTITY_SCHEMA",
    "DEFAULT_CALLABLE_EXPANSION_DEPTH",
    "DEFAULT_CALLABLE_EXPANSION_NODES",
    "CallableExpansionError",
    "CallableReachabilityError",
    "CallableUse",
    "CallableKind",
    "CallableMetadata",
    "source_function_metadata",
    "expand_callable_calls",
    "callable_uses",
    "reachable_callable_definitions",
    "reachable_module_callables",
    "stable_callee_identity",
]
