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
from zlang.ir import expressions as expr
from zlang.ir.functional_regions import (
    CompileTimeBinderRef,
    ExactReductionCombine,
    ExactReductionOperation,
)
from zlang.ir.types import HardwareType


CALLABLE_IDENTITY_SCHEMA = "zlang-callable-definition-v1"
DEFAULT_CALLABLE_EXPANSION_DEPTH = 64
DEFAULT_CALLABLE_EXPANSION_NODES = 65_536


class CallableKind(str, Enum):
    FUNCTION = "function"
    OPERATOR = "operator"


@dataclass(frozen=True, init=False)
class CallableMetadata:
    """Semantic provenance for one monomorphic callable definition.

    ``arguments`` uses the existing rendered generic-argument spelling.  A
    concrete specialization may retain its pre-existing specialization digest;
    otherwise the callable identity is derived from this metadata and the exact
    typed signature.
    """

    kind: CallableKind
    source_name: str
    # Stored under the conventional diagnostic-provenance spelling so
    # canonical identity rendering omits relocation-only source paths.  The
    # stable specialization/callee identity remains semantic.
    source_identity: str
    specialization_identity: str | None = None
    arguments: tuple[tuple[str, str], ...] = ()

    def __init__(
        self,
        kind: CallableKind,
        source_name: str,
        declaration_identity: str | None = None,
        specialization_identity: str | None = None,
        arguments: tuple[tuple[str, str], ...] = (),
        *,
        source_identity: str | None = None,
    ) -> None:
        if declaration_identity is not None and source_identity is not None:
            raise ValueError(
                "callable metadata cannot specify two declaration identities"
            )
        declaration = declaration_identity or source_identity or ""
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source_name", source_name)
        object.__setattr__(self, "source_identity", declaration)
        object.__setattr__(self, "specialization_identity", specialization_identity)
        object.__setattr__(self, "arguments", arguments)
        self.__post_init__()

    @property
    def declaration_identity(self) -> str:
        return self.source_identity

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
    name: str,
    parameters: tuple[object, ...],
    return_type: HardwareType,
    metadata: CallableMetadata | None = None,
) -> str:
    """Return the stable semantic identity of one exact callable signature."""

    if metadata is not None and metadata.specialization_identity is not None:
        return metadata.specialization_identity
    provenance = (
        {
            "kind": metadata.kind.value,
            "source_name": metadata.source_name,
            "declaration_identity": metadata.declaration_identity,
            "arguments": metadata.arguments,
        }
        if metadata is not None
        else {
            "kind": CallableKind.FUNCTION.value,
            "source_name": name,
            "declaration_identity": f"legacy:{name}",
            "arguments": (),
        }
    )
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


def _callable_definition_value(value: object) -> object:
    """Render one callable definition modulo bound local identities.

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

    def bound_name(names: dict[str, str], identity: str, prefix: str) -> str:
        current = names.get(identity)
        if current is None:
            current = f"{prefix}{len(names)}"
            names[identity] = current
        return current

    def render(current: object) -> object:
        if isinstance(current, CompileTimeBinderRef):
            return {
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
        if isinstance(current, expr.FunctionalCaptureRef):
            return {
                "$type": (
                    f"{type(current).__module__}.{type(current).__qualname__}"
                ),
                "fields": [
                    ["identity", bound_name(capture_names, current.identity, "c")],
                    ["display_name", current.display_name],
                    ["type", render(current.type)],
                ],
            }
        if isinstance(current, Enum):
            return {
                "$enum": f"{type(current).__module__}.{type(current).__qualname__}",
                "value": current.value,
            }
        if is_dataclass(current) and not isinstance(current, type):
            return {
                "$type": f"{type(current).__module__}.{type(current).__qualname__}",
                "fields": [
                    [item.name, render(getattr(current, item.name))]
                    for item in fields(current)
                    if item.name not in {"origin", "source_origin"}
                ],
            }
        if isinstance(current, tuple):
            return [render(item) for item in current]
        if isinstance(current, list):
            return [render(item) for item in current]
        if isinstance(current, dict):
            return {
                str(key): render(item)
                for key, item in sorted(current.items(), key=lambda pair: str(pair[0]))
            }
        if current is None or isinstance(current, (str, int, bool)):
            return current
        return str(current)

    return render(value)


def _callable_definition_order_key(value: object) -> str:
    """Return a deterministic key for equivalent physical definitions.

    The key intentionally retains concrete callee identities.  It is not an
    equivalence relation; it merely chooses the same representative when a
    semantically equivalent definition was published by several hierarchy
    contexts in a different discovery order.
    """

    return repr(_callable_definition_value(value))


def _callable_definitions_equivalent(
    left: object,
    right: object,
    definitions: dict[str, tuple[object, ...]],
    *,
    seen: set[tuple[int, int]] | None = None,
) -> bool:
    """Compare typed callable definitions modulo resolved call identities.

    Generic specialization identities include the dependency closure of the
    physical module context that requested them.  Consequently one ordinary
    source function may be published in a parent and a child with identical
    executable semantics while its body calls two differently identified (but
    structurally equal) generic specializations.  Raw dataclass equality makes
    that valid hierarchy look like a conflicting definition.

    Resolve those call edges and compare their definitions recursively.  This
    is deliberately stricter than comparing source names: exact signatures,
    bodies, metadata provenance, tables, and types must still agree.  Truly
    different bodies carrying one identity therefore remain an error.
    """

    compared = seen if seen is not None else set()
    pair = (id(left), id(right))
    if pair in compared:
        # Reachable recursive call graphs are rejected by the normal DFS.  A
        # co-inductive guard here only keeps duplicate validation bounded; it
        # does not make recursion executable.
        return True
    compared.add(pair)

    def representative(identity: str) -> object | None:
        candidates = definitions.get(identity, ())
        if not candidates:
            return None
        return min(candidates, key=_callable_definition_order_key)

    def callable_reference_equal(
        left_name: str | None,
        left_identity: str | None,
        right_name: str | None,
        right_identity: str | None,
    ) -> bool:
        if left_identity is None or right_identity is None:
            return left_identity == right_identity and left_name == right_name
        left_definition = representative(left_identity)
        right_definition = representative(right_identity)
        if left_definition is None or right_definition is None:
            # Unknown edges are diagnosed if reachable.  Until then retain
            # their exact identity so duplicate validation cannot hide one.
            return left_identity == right_identity and left_name == right_name
        # A typed call carries both the stable callee identity and the helper
        # name consumed by backends.  Each name must agree with its own
        # resolved definition even when the two context-specific helper names
        # differ from one another.  Otherwise a malformed call could be hidden
        # by selecting the other duplicate definition as the representative.
        if (
            left_name != getattr(left_definition, "name", None)
            or right_name != getattr(right_definition, "name", None)
        ):
            return False
        return _callable_definitions_equivalent(
            left_definition,
            right_definition,
            definitions,
            seen=compared,
        )

    left_binders: dict[str, str] = {}
    right_binders: dict[str, str] = {}
    left_captures: dict[str, str] = {}
    right_captures: dict[str, str] = {}

    def alpha_equal(
        left_identity: str,
        right_identity: str,
        forward: dict[str, str],
        reverse: dict[str, str],
    ) -> bool:
        mapped = forward.get(left_identity)
        if mapped is not None:
            return mapped == right_identity
        if right_identity in reverse:
            return False
        forward[left_identity] = right_identity
        reverse[right_identity] = left_identity
        return True

    def metadata_equal(
        left_metadata: CallableMetadata | None,
        right_metadata: CallableMetadata | None,
    ) -> bool:
        if left_metadata is None or right_metadata is None:
            return left_metadata is right_metadata
        return (
            left_metadata.kind == right_metadata.kind
            and left_metadata.source_name == right_metadata.source_name
            and left_metadata.declaration_identity
            == right_metadata.declaration_identity
            and left_metadata.arguments == right_metadata.arguments
        )

    def value_equal(left_value: object, right_value: object) -> bool:
        if type(left_value) is not type(right_value):
            return False
        if isinstance(left_value, CompileTimeBinderRef):
            return (
                left_value.display_name == right_value.display_name
                and left_value.start == right_value.start
                and left_value.stop == right_value.stop
                and alpha_equal(
                    left_value.identity,
                    right_value.identity,
                    left_binders,
                    right_binders,
                )
            )
        if isinstance(left_value, expr.FunctionalCaptureRef):
            return (
                left_value.display_name == right_value.display_name
                and value_equal(left_value.type, right_value.type)
                and alpha_equal(
                    left_value.identity,
                    right_value.identity,
                    left_captures,
                    right_captures,
                )
            )
        if isinstance(left_value, expr.Call):
            return (
                value_equal(left_value.arguments, right_value.arguments)
                and value_equal(left_value.type, right_value.type)
                and callable_reference_equal(
                    left_value.function,
                    left_value.callee_identity,
                    right_value.function,
                    right_value.callee_identity,
                )
            )
        if isinstance(left_value, (ExactReductionCombine, ExactReductionOperation)):
            for item in fields(left_value):
                if item.name in {"function", "callee_identity"}:
                    continue
                if not value_equal(
                    getattr(left_value, item.name),
                    getattr(right_value, item.name),
                ):
                    return False
            return callable_reference_equal(
                left_value.function,
                left_value.callee_identity,
                right_value.function,
                right_value.callee_identity,
            )
        if isinstance(left_value, CallableMetadata):
            return metadata_equal(left_value, right_value)
        if isinstance(left_value, Enum):
            return left_value == right_value
        if is_dataclass(left_value) and not isinstance(left_value, type):
            for item in fields(left_value):
                if item.name in {"origin", "source_origin"}:
                    continue
                if not value_equal(
                    getattr(left_value, item.name),
                    getattr(right_value, item.name),
                ):
                    return False
            return True
        if isinstance(left_value, (tuple, list)):
            return len(left_value) == len(right_value) and all(
                value_equal(left_item, right_item)
                for left_item, right_item in zip(
                    left_value, right_value, strict=True
                )
            )
        if isinstance(left_value, dict):
            return left_value.keys() == right_value.keys() and all(
                value_equal(left_value[key], right_value[key])
                for key in left_value
            )
        return left_value == right_value

    left_metadata = getattr(left, "metadata", None)
    right_metadata = getattr(right, "metadata", None)
    if not metadata_equal(left_metadata, right_metadata):
        return False
    # Generated specialization names contain the context-derived identity.
    # Source-named/legacy callables retain their exact public helper name.
    if left_metadata is None and getattr(left, "name", None) != getattr(
        right, "name", None
    ):
        return False
    for attribute in ("parameters", "return_type", "body"):
        if not value_equal(getattr(left, attribute), getattr(right, attribute)):
            return False
    return True


class CallableExpansionError(ValueError):
    """A typed call graph cannot be expanded within the requested bounds."""


class CallableReachabilityError(ValueError):
    """Executable typed IR references an invalid callable graph."""


@dataclass(frozen=True)
class CallableUse:
    """One identity-bearing call edge retained by executable typed IR."""

    function: str
    callee_identity: str | None


def callable_uses(value: object) -> tuple[CallableUse, ...]:
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

    def visit(current: object) -> None:
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
    by_identity: dict[str, object] = {}
    for identity in sorted(grouped_definitions):
        candidates = grouped_definitions[identity]
        representative = min(candidates, key=_callable_definition_order_key)
        for definition in candidates:
            if (
                getattr(definition, "name", None)
                != getattr(representative, "name", None)
                or not _callable_definitions_equivalent(
                    representative,
                    definition,
                    grouped_definitions,
                )
            ):
                raise CallableReachabilityError(
                    f"callable identity '{identity}' has conflicting definitions"
                )
        by_identity[identity] = representative

    by_name: dict[str, list[object]] = {}
    for definition in by_identity.values():
        name = str(getattr(definition, "name"))
        by_name.setdefault(name, []).append(definition)

    def use_key(use: CallableUse) -> tuple[str, str]:
        return (use.callee_identity or f"name:{use.function}", use.function)

    def resolve(use: CallableUse) -> object:
        if use.callee_identity is not None:
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
        candidates = by_name.get(use.function, ())
        if len(candidates) != 1:
            detail = "unknown" if not candidates else "ambiguous"
            raise CallableReachabilityError(
                f"{detail} legacy typed function call '{use.function}'"
            )
        return candidates[0]

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
            callable_uses(getattr(definition, "body")),
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
            for use in callable_uses(root)
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
    return reachable_callable_definitions(definitions, roots)


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
    by_name: dict[str, list[object]] = {}
    for definition in ordered:
        identity = str(getattr(definition, "callee_identity", ""))
        name = str(getattr(definition, "name", ""))
        if not identity or not name:
            raise CallableExpansionError(
                "callable definition requires a name and stable callee identity"
            )
        previous = by_identity.get(identity)
        if previous is not None:
            raise CallableExpansionError(
                f"duplicate callable identity '{identity}'"
            )
        by_identity[identity] = definition
        by_name.setdefault(name, []).append(definition)

    nodes = 0

    def account() -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise CallableExpansionError(
                f"callable expansion exceeds {max_nodes} expression nodes"
            )

    def resolve(call: expr.Call) -> object:
        if call.callee_identity is not None:
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
        candidates = by_name.get(call.function, ())
        if len(candidates) != 1:
            detail = "unknown" if not candidates else "ambiguous"
            raise CallableExpansionError(
                f"{detail} legacy callable name '{call.function}'"
            )
        return candidates[0]

    def map_value(value: object, stack: tuple[str, ...]) -> object:
        if isinstance(value, expr.Expression):
            return visit(value, stack)
        if isinstance(value, tuple):
            return tuple(map_value(item, stack) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            updates = {
                item.name: map_value(getattr(value, item.name), stack)
                for item in fields(value)
                if item.init and item.name not in {"type", "origin"}
            }
            try:
                return replace(value, **updates)
            except (TypeError, ValueError):
                return value
        return value

    def substitute(value: object, bindings: dict[str, expr.Expression]) -> object:
        if isinstance(value, expr.ParameterRef) and value.name in bindings:
            return bindings[value.name]
        if isinstance(value, expr.Expression):
            updates = {
                item.name: substitute(getattr(value, item.name), bindings)
                for item in fields(value)
                if item.init and item.name not in {"type", "origin"}
            }
            return replace(value, **updates) if updates else value
        if isinstance(value, tuple):
            return tuple(substitute(item, bindings) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            updates = {
                item.name: substitute(getattr(value, item.name), bindings)
                for item in fields(value)
                if item.init and item.name not in {"type", "origin"}
            }
            try:
                return replace(value, **updates)
            except (TypeError, ValueError):
                return value
        return value

    def visit(value: expr.Expression, stack: tuple[str, ...]) -> expr.Expression:
        account()
        if isinstance(value, expr.Reduce):
            # ``Reduce.expanded`` is a frozen exact-overload implementation,
            # not another high-level operand.  Consumers such as M32 and the
            # e-graph must continue to see the nominal reduction boundary and
            # must not accidentally inline or reinterpret that operator tree.
            # Parameter substitution has already crossed this field when the
            # Reduce itself came from a callable body, so leaving it opaque here
            # cannot strand formal parameters.
            collection = map_value(value.collection, stack)
            return replace(value, collection=collection)
        if not isinstance(value, expr.Call):
            updates = {
                item.name: map_value(getattr(value, item.name), stack)
                for item in fields(value)
                if item.init and item.name not in {"type", "origin"}
            }
            return replace(value, **updates) if updates else value
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
        if value.origin is not None:
            expanded = replace(expanded, origin=value.origin)
        return expanded

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
    "expand_callable_calls",
    "callable_uses",
    "reachable_callable_definitions",
    "reachable_module_callables",
    "stable_callee_identity",
]
