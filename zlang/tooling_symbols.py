# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable tooling records and compiler-owned document-symbol projection."""


from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from zlang.ast import nodes as ast_nodes
from zlang.ir.types import (
    BitType,
    BitsType,
    FixedType,
    SIntType,
    UFixedType,
    UIntType,
)
from zlang.parser import ParseError, parse


_SourceRecord = TypeVar("_SourceRecord")


@dataclass(frozen=True)
class SourceFacts:
    imports: tuple[str, ...]
    declarations: tuple[str, ...]
    parsed: bool

@dataclass(frozen=True)
class ToolingOrigin:
    source_unit: str | None
    construct: str | None
    start_line: int
    start_column: int
    end_line: int
    end_column: int

@dataclass(frozen=True)
class ToolingHover:
    """Narrow compiler-owned facts for one semantic hover result."""

    name: str | None
    kind: str
    type_text: str | None
    width: int | None
    signedness: str | None
    fixed_point: str | None
    port_direction: str | None
    signature: str | None
    origin: ToolingOrigin | None

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("tooling hover kind must not be empty")

@dataclass(frozen=True)
class ToolingDefinition:
    """Narrow compiler-owned target projection for go-to-definition."""

    name: str
    kind: str
    target_path: Path
    target_origin: ToolingOrigin

    def __post_init__(self) -> None:
        if not self.name or not self.kind:
            raise ValueError("tooling definition identity must not be empty")
        if not self.target_path.is_absolute():
            raise ValueError("tooling definition path must be absolute")

@dataclass(frozen=True)
class ToolingReference:
    """One compiler-owned reference occurrence projected for an editor."""

    source_path: Path
    origin: ToolingOrigin

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise ValueError("tooling reference path must be absolute")

@dataclass(frozen=True)
class ToolingRenameEdit:
    """One exact compiler-owned source edit for a safe rename."""

    source_path: Path
    origin: ToolingOrigin
    new_text: str

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise ValueError("tooling rename path must be absolute")
        if not self.new_text:
            raise ValueError("tooling rename text must not be empty")

@dataclass(frozen=True)
class ToolingCompletion:
    """One compiler-visible semantic completion candidate."""

    name: str
    kind: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.kind:
            raise ValueError("tooling completion identity must not be empty")

@dataclass(frozen=True)
class ToolingSignatureHelp:
    """One compiler-resolved callable signature for an editor position."""

    label: str
    parameters: tuple[str, ...]
    active_parameter: int
    origin: ToolingOrigin | None

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("tooling signature label must not be empty")
        if (
            isinstance(self.active_parameter, bool)
            or not isinstance(self.active_parameter, int)
            or self.active_parameter < 0
            or (
                self.parameters
                and self.active_parameter >= len(self.parameters)
            )
        ):
            raise ValueError("tooling signature active parameter is invalid")

_SEMANTIC_TOKEN_KINDS = frozenset({"function", "parameter", "property", "variable"})
_SEMANTIC_TOKEN_MODIFIERS = frozenset({"declaration"})


@dataclass(frozen=True)
class ToolingSemanticToken:
    """One exact compiler-owned semantic source occurrence.

    Token kinds and modifiers are protocol-independent names.  Numeric LSP
    legend indices and relative encoding remain owned by ``zlang.lsp``.
    """

    origin: ToolingOrigin
    kind: str
    modifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _SEMANTIC_TOKEN_KINDS:
            raise ValueError(f"unsupported tooling semantic token kind: {self.kind}")
        if any(item not in _SEMANTIC_TOKEN_MODIFIERS for item in self.modifiers):
            raise ValueError("unsupported tooling semantic token modifier")
        if len(set(self.modifiers)) != len(self.modifiers):
            raise ValueError("tooling semantic token modifiers must be unique")
        if (
            self.origin.start_line != self.origin.end_line
            or self.origin.end_column <= self.origin.start_column
        ):
            raise ValueError("tooling semantic token requires an exact single-line span")

def _origin_from_source(
    origin: object | None,
    *,
    construct: str | None = None,
) -> ToolingOrigin | None:
    if origin is None:
        return None
    span = getattr(origin, "span", origin)
    if not all(
        hasattr(span, attribute)
        for attribute in ("start_line", "start_column", "end_line", "end_column")
    ):
        return None
    return ToolingOrigin(
        getattr(origin, "source_unit", None),
        construct if construct is not None else getattr(origin, "construct", None),
        int(span.start_line),
        int(span.start_column),
        int(span.end_line),
        int(span.end_column),
    )


def _position_in_origin(
    origin: ToolingOrigin | None,
    line: int,
    character: int,
) -> bool:
    if origin is None:
        return False
    position = (line + 1, character + 1)
    return (origin.start_line, origin.start_column) <= position < (
        origin.end_line,
        origin.end_column,
    )


def _narrowest_source_record(
    records: Iterable[_SourceRecord],
    *,
    source_unit: str | None,
    line: int,
    character: int,
    source_origin: Callable[[_SourceRecord], object | None],
    include_column_span: bool = True,
) -> _SourceRecord | None:
    """Select the most-specific compiler record covering one source position."""

    candidates: list[tuple[tuple[int, int, int], _SourceRecord]] = []
    for index, record in enumerate(records):
        compiler_origin = source_origin(record)
        origin = _origin_from_source(compiler_origin)
        if origin is None:
            continue
        if (
            source_unit is not None
            and getattr(compiler_origin, "source_unit", None)
            not in {None, source_unit}
        ):
            continue
        if not _position_in_origin(origin, line, character):
            continue
        line_span = origin.end_line - origin.start_line
        column_span = (
            origin.end_column - origin.start_column
            if include_column_span and line_span == 0
            else 1_000_000
        )
        candidates.append(((line_span, column_span, index), record))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None

def _type_metadata(
    type_value: object,
) -> tuple[str, int | None, str | None, str | None]:
    type_text = str(type_value)
    width = getattr(type_value, "width", None)
    width = int(width) if isinstance(width, int) else None
    if isinstance(type_value, (SIntType, FixedType)):
        signedness = "signed"
    elif isinstance(type_value, (UIntType, UFixedType)):
        signedness = "unsigned"
    elif isinstance(type_value, BitsType):
        signedness = "bit-vector"
    elif isinstance(type_value, BitType):
        signedness = "bit"
    else:
        signedness = None
    fixed_point = (
        type_text if isinstance(type_value, (FixedType, UFixedType)) else None
    )
    return type_text, width, signedness, fixed_point

def _hover_record(
    *,
    name: str | None,
    kind: str,
    type_value: object | None,
    origin: object | None,
    port_direction: str | None = None,
    signature: str | None = None,
) -> ToolingHover:
    if type_value is None:
        type_text = width = signedness = fixed_point = None
    else:
        type_text, width, signedness, fixed_point = _type_metadata(type_value)
    return ToolingHover(
        name,
        kind,
        type_text,
        width,
        signedness,
        fixed_point,
        port_direction,
        signature,
        _origin_from_source(origin),
    )

@dataclass(frozen=True)
class ToolingDiagnostic:
    code: str
    message: str
    primary: ToolingOrigin | None
    notes: tuple[str, ...]
    fixes: tuple[str, ...]
    machine_fixes: tuple[ToolingDiagnosticFix, ...] = ()
    severity: str = "error"

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning"}:
            raise ValueError("tooling diagnostic severity is unsupported")

@dataclass(frozen=True)
class ToolingDiagnosticEdit:
    """One exact current-source edit projected from compiler fix metadata."""

    source_path: Path
    origin: ToolingOrigin
    replacement: str

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise ValueError("tooling diagnostic edit path must be absolute")
        if not isinstance(self.replacement, str):
            raise TypeError("tooling diagnostic replacement must be a string")

@dataclass(frozen=True)
class ToolingDiagnosticFix:
    """One atomic machine-applicable compiler diagnostic fix."""

    title: str
    edits: tuple[ToolingDiagnosticEdit, ...]

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("tooling diagnostic fix title must not be empty")
        if not self.edits:
            raise ValueError("tooling diagnostic fix requires at least one edit")
        if any(not isinstance(edit, ToolingDiagnosticEdit) for edit in self.edits):
            raise TypeError(
                "tooling diagnostic fix edits must be edit records"
            )

@dataclass(frozen=True)
class ToolingSymbol:
    """Compiler-parser-owned structure projected for document symbols only.

    This is intentionally not an AST or typed-IR export.  ``range`` and
    ``selection_range`` are compiler source origins; when a legacy AST node
    does not retain a narrower origin, the containing declaration origin is
    used rather than estimating character offsets from source text.
    """

    name: str
    kind: str
    range: ToolingOrigin | None
    selection_range: ToolingOrigin | None
    children: tuple["ToolingSymbol", ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tooling symbol name must not be empty")
        if not self.kind:
            raise ValueError("tooling symbol kind must not be empty")

def _symbol_origin(node: object, kind: str, name: str) -> ToolingOrigin | None:
    return _origin_from_source(
        getattr(node, "origin", None),
        construct=f"{kind} {name}",
    )

def _enclosing_origin(
    name: str,
    kind: str,
    origins: tuple[ToolingOrigin, ...],
) -> ToolingOrigin | None:
    """Build a non-guessing declaration envelope from known child spans."""

    if not origins:
        return None
    start = min(
        origins,
        key=lambda value: (value.start_line, value.start_column),
    )
    end = max(
        origins,
        key=lambda value: (value.end_line, value.end_column),
    )
    return ToolingOrigin(
        None,
        f"{kind} {name}",
        start.start_line,
        start.start_column,
        end.end_line,
        end.end_column,
    )

def _make_symbol(
    name: str,
    kind: str,
    node: object | None = None,
    *,
    fallback: ToolingOrigin | None = None,
    children: tuple[ToolingSymbol, ...] = (),
) -> ToolingSymbol:
    origin = (
        None
        if node is None
        else _symbol_origin(node, kind, name)
    ) or fallback
    return ToolingSymbol(name, kind, origin, origin, children)

def _children_for_struct(
    fields: tuple[object, ...], fallback: ToolingOrigin | None
) -> tuple[ToolingSymbol, ...]:
    return tuple(
        _make_symbol(
            str(getattr(field, "name")),
            "field",
            field,
            fallback=fallback,
        )
        for field in fields
    )

def _children_for_enum(
    members: tuple[str, ...], fallback: ToolingOrigin | None
) -> tuple[ToolingSymbol, ...]:
    return tuple(
        _make_symbol(member, "enum_member", fallback=fallback)
        for member in members
    )

def _symbol_for_item(
    item: object,
    fallback: ToolingOrigin | None,
) -> tuple[ToolingSymbol, ...]:
    """Project one parser-owned declaration without source-text heuristics."""

    if isinstance(item, tuple):
        # Clock/reset declarations retain their physical declaration object in
        # the ordered parser item.  Other tagged tuples are not named symbols.
        if len(item) >= 3 and item[0] in {"clock", "reset"}:
            declaration = item[-1]
            return (
                _make_symbol(str(item[1]), "field", declaration, fallback=fallback),
            )
        return ()
    if isinstance(item, ast_nodes.PortDecl):
        names = item.names or (item.name,)
        return tuple(
            _make_symbol(name, "field", item, fallback=fallback)
            for name in names
        )
    if isinstance(item, ast_nodes.TypeAlias):
        return (_make_symbol(item.name, "type", item, fallback=fallback),)
    if isinstance(item, ast_nodes.StructDecl):
        symbol = _make_symbol(
            item.name,
            "struct",
            item,
            fallback=fallback,
            children=_children_for_struct(item.fields, fallback),
        )
        return (symbol,)
    if isinstance(item, ast_nodes.EnumDecl):
        return (_make_symbol(
            item.name,
            "enum",
            item,
            fallback=fallback,
            children=_children_for_enum(item.members, fallback),
        ),)
    if isinstance(item, ast_nodes.TaggedUnionDecl):
        children = tuple(
            _make_symbol(
                variant.name,
                "struct",
                variant,
                fallback=fallback,
                children=_children_for_struct(variant.fields, fallback),
            )
            for variant in item.variants
        )
        return (_make_symbol(
            item.name, "class", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.FunctionDecl):
        return (_make_symbol(item.name, "function", item, fallback=fallback),)
    if isinstance(item, ast_nodes.OperatorDecl):
        return (_make_symbol(
            item.operator, "operator", item, fallback=fallback
        ),)
    if isinstance(item, ast_nodes.ModuleInterfaceDecl):
        children = tuple(
            _make_symbol(port.name, "field", port, fallback=fallback)
            for port in item.ports
            for _ in (0,)
        )
        return (_make_symbol(
            item.name, "interface", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.ProtocolDecl):
        children = tuple(
            _make_symbol(channel.name, "field", fallback=fallback)
            for channel in item.channels
        )
        return (_make_symbol(
            item.name, "interface", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.ResourceDefinitionDecl):
        children = tuple(
            _make_symbol(port.name, "field", fallback=fallback)
            for port in item.ports
        )
        return (_make_symbol(
            item.name, "class", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.TargetFamilyDecl):
        return (_make_symbol(item.name, "class", item, fallback=fallback),)
    if isinstance(item, ast_nodes.TargetInstanceDecl):
        return (_make_symbol(item.name, "object", item, fallback=fallback),)
    if isinstance(item, ast_nodes.ArchitectureTemplateDecl):
        return (_make_symbol(item.name, "class", item, fallback=fallback),)
    if isinstance(item, ast_nodes.ModuleParameter):
        return (_make_symbol(
            item.name,
            "type_parameter" if item.kind == "type" else "variable",
            item,
            fallback=fallback,
        ),)
    if isinstance(item, ast_nodes.RegisterDecl):
        return (_make_symbol(item.name, "field", item, fallback=fallback),)
    if isinstance(item, ast_nodes.RequestResponseDecl):
        return (_make_symbol(item.name, "field", item, fallback=fallback),)
    if isinstance(item, ast_nodes.CsrBlockDecl):
        children = tuple(
            _make_symbol(
                register.name,
                "field",
                register,
                fallback=fallback,
                children=tuple(
                    _make_symbol(field.name, "field", field, fallback=fallback)
                    for field in register.fields
                ),
            )
            for register in item.registers
        )
        return (_make_symbol(
            item.name, "namespace", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.RuleDecl):
        return (_make_symbol(item.name, "method", item, fallback=fallback),)
    if isinstance(item, ast_nodes.FsmDecl):
        return (_make_symbol(
            item.name,
            "class",
            item,
            fallback=fallback,
            children=tuple(
                _make_symbol(state.member, "enum_member", state, fallback=fallback)
                for state in item.states
            ),
        ),)
    if isinstance(item, ast_nodes.FifoDecl):
        return (_make_symbol(item.name, "field", item, fallback=fallback),)
    if isinstance(item, (ast_nodes.MemoryDecl, ast_nodes.RomDecl)):
        return (_make_symbol(item.name, "field", item, fallback=fallback),)
    # ArbiterDecl carries source semantics but intentionally has no declaration
    # name in the AST (its destination is a connection endpoint, not a symbol
    # identity).  Do not invent a name for document symbols.
    if isinstance(item, ast_nodes.InstanceDecl):
        return (_make_symbol(item.name, "object", item, fallback=fallback),)
    if isinstance(item, ast_nodes.AggregateInterfaceDecl):
        return (_make_symbol(item.name, "interface", item, fallback=fallback),)
    if isinstance(item, ast_nodes.GenericDeclaration):
        return (_make_symbol(item.name, "object", item, fallback=fallback),)
    if isinstance(item, ast_nodes.VerificationScopeDecl):
        children = tuple(
            _make_symbol(goal.name, "event", goal, fallback=fallback)
            for goal in item.goals
        )
        return (_make_symbol(
            item.name, "namespace", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.VerificationGoalDecl):
        return (_make_symbol(item.name, "event", item, fallback=fallback),)
    if isinstance(item, ast_nodes.EquivDecl):
        return (_make_symbol(item.name, "event", item, fallback=fallback),)
    if isinstance(item, ast_nodes.Assignment):
        return (_make_symbol(item.target, "variable", item, fallback=fallback),)
    if isinstance(item, ast_nodes.ModuleTimingDecl):
        return ()
    return ()

def _module_symbols(module: ast_nodes.Module) -> tuple[ToolingSymbol, ...]:
    if module.ordered_items:
        items: tuple[object, ...] = (
            *(module.declared_parameters or module.parameters),
            *module.ordered_items,
        )
    else:
        # Declaration-only units use the parser's category carriers instead of
        # a module body.  Keep this fallback structural and deterministic; it
        # does not inspect source text or recreate grammar rules.
        items = (
            *(module.declared_parameters or module.parameters),
            *module.type_aliases,
            *module.enums,
            *module.tagged_unions,
            *module.structs,
            *module.functions,
            *module.operators,
            *module.protocols,
            *module.resource_definitions,
            *module.target_families,
            *module.target_instances,
            *module.architecture_templates,
            *module.module_interfaces,
        )
    known_origins = tuple(
        origin
        for item in items
        for origin in (
            _symbol_origin(item, type(item).__name__, str(getattr(item, "name", "")))
            if not isinstance(item, tuple)
            else None,
        )
        if origin is not None
    )
    module_origin = _enclosing_origin(module.name, "module", known_origins)
    if items:
        children: list[ToolingSymbol] = []
        for item in items:
            if isinstance(item, ast_nodes.Assignment):
                declared = {
                    name
                    for port in module.ports
                    for name in (port.names or (port.name,))
                }
                declared.update(
                    str(getattr(value, "name"))
                    for value in (
                        *module.registers,
                        *module.fifos,
                        *module.memories,
                        *module.roms,
                        *module.instances,
                    )
                )
                if item.target.split(".", 1)[0] in declared:
                    continue
            children.extend(_symbol_for_item(item, module_origin))
    else:
        children = []
    return (
        _make_symbol(
            module.name,
            "module",
            fallback=module_origin,
            children=tuple(children),
        ),
    )

def document_symbols(source_text: str) -> tuple[ToolingSymbol, ...]:
    """Return parser-owned document symbols for the current source text.

    Parse failures intentionally return an empty result.  Diagnostics remain
    the existing ``check_snapshot`` responsibility; no fallback scanner is
    used while a document is incomplete.
    """

    try:
        syntax = parse(source_text)
    except ParseError:
        return ()
    if syntax.declaration_only:
        carrier = _module_symbols(syntax)[0]
        symbols = list(carrier.children)
    else:
        symbols = list(_module_symbols(syntax))
    symbols.extend(
        symbol for item in syntax.submodules for symbol in _module_symbols(item)
    )
    return tuple(symbols)
