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
        # None identifies the standalone root, not a wildcard over imports.
        # Imported callable bodies can retain overlapping line/column spans;
        # they must never win a root-document navigation query by being narrower.
        if getattr(compiler_origin, "source_unit", None) not in {None, source_unit}:
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
    # Presentation-only binding retained from the compiler diagnostic. These
    # fields do not participate in semantic or hardware identities.
    source_path: Path | None = None
    source_digest: str | None = None

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
    ``selection_range`` are compiler source origins.  Nested symbols are
    published only from exact parser-owned spans: substituting a container's
    range for a child corrupts VS Code Outline and Sticky Scroll ownership.
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
    selection = (
        None
        if node is None
        else _origin_from_source(
            getattr(node, "name_origin", None),
            construct=f"{kind} {name}",
        )
    ) or origin
    return ToolingSymbol(name, kind, origin, selection, children)


def _symbols_for_port(port: ast_nodes.PortDecl) -> tuple[ToolingSymbol, ...]:
    """Project grouped ports without publishing overlapping sibling ranges."""

    names = port.names or (port.name,)
    symbols: list[ToolingSymbol] = []
    for index, name in enumerate(names):
        name_origin = (
            _origin_from_source(
                port.name_origins[index], construct=f"field {name}"
            )
            if index < len(port.name_origins)
            else None
        )
        # A grouped declaration has one shared declaration span.  Its exact
        # lexer-owned name spans are the only non-overlapping child ranges.
        range_origin = name_origin if len(names) > 1 else (
            _symbol_origin(port, "field", name) or name_origin
        )
        symbols.append(
            ToolingSymbol(name, "field", range_origin, name_origin or range_origin)
        )
    return tuple(symbols)

def _children_for_struct(fields: tuple[object, ...]) -> tuple[ToolingSymbol, ...]:
    return tuple(
        _make_symbol(
            str(getattr(field, "name")),
            "field",
            field,
        )
        for field in fields
    )

def _children_for_enum(
    declaration: ast_nodes.EnumDecl,
) -> tuple[ToolingSymbol, ...]:
    """Project enum members with their parser-owned name spans.

    Falling back to the declaration-unit envelope made every member of every
    top-level enum overlap its siblings.  VS Code consumes document-symbol
    ranges for Outline and Sticky Scroll, so those invalid child ranges could
    pin an unrelated enum while the viewport moved.  The parser already owns
    exact member spans; do not reconstruct them from source text.
    """

    symbols: list[ToolingSymbol] = []
    for index, member in enumerate(declaration.members):
        span = (
            declaration.member_origins[index]
            if index < len(declaration.member_origins)
            else None
        )
        origin = _origin_from_source(
            span,
            construct=f"enum_member {member}",
        )
        symbols.append(ToolingSymbol(member, "enum_member", origin, origin))
    return tuple(symbols)


def _position(origin: ToolingOrigin) -> tuple[int, int, int, int]:
    return (
        origin.start_line,
        origin.start_column,
        origin.end_line,
        origin.end_column,
    )


def _contains(parent: ToolingOrigin, child: ToolingOrigin) -> bool:
    parent_start = (parent.start_line, parent.start_column)
    parent_end = (parent.end_line, parent.end_column)
    child_start = (child.start_line, child.start_column)
    child_end = (child.end_line, child.end_column)
    return parent_start <= child_start < child_end <= parent_end


def _overlaps(left: ToolingOrigin, right: ToolingOrigin) -> bool:
    left_start = (left.start_line, left.start_column)
    left_end = (left.end_line, left.end_column)
    right_start = (right.start_line, right.start_column)
    right_end = (right.end_line, right.end_column)
    return left_start < right_end and right_start < left_end


def _validated_symbol(symbol: ToolingSymbol) -> ToolingSymbol | None:
    """Fail closed on ranges that can make one symbol own unrelated source."""

    if symbol.range is None:
        return None
    selection = symbol.selection_range
    if selection is None or not _contains(symbol.range, selection):
        selection = symbol.range
    children: list[ToolingSymbol] = []
    for child in sorted(
        (value for value in map(_validated_symbol, symbol.children) if value),
        key=lambda value: _position(value.range),  # type: ignore[arg-type]
    ):
        assert child.range is not None
        if (
            _position(child.range) == _position(symbol.range)
            or not _contains(symbol.range, child.range)
        ):
            continue
        if children and _overlaps(children[-1].range, child.range):  # type: ignore[arg-type]
            continue
        children.append(child)
    return ToolingSymbol(
        symbol.name, symbol.kind, symbol.range, selection, tuple(children)
    )


def _validated_symbols(
    symbols: tuple[ToolingSymbol, ...],
) -> tuple[ToolingSymbol, ...]:
    result: list[ToolingSymbol] = []
    for symbol in sorted(
        (value for value in map(_validated_symbol, symbols) if value),
        key=lambda value: _position(value.range),  # type: ignore[arg-type]
    ):
        assert symbol.range is not None
        if result and _overlaps(result[-1].range, symbol.range):  # type: ignore[arg-type]
            continue
        result.append(symbol)
    return tuple(result)

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
        return _symbols_for_port(item)
    if isinstance(item, ast_nodes.TypeAlias):
        return (_make_symbol(item.name, "type", item, fallback=fallback),)
    if isinstance(item, ast_nodes.StructDecl):
        symbol = _make_symbol(
            item.name,
            "struct",
            item,
            fallback=fallback,
            children=_children_for_struct(item.fields),
        )
        return (symbol,)
    if isinstance(item, ast_nodes.EnumDecl):
        return (_make_symbol(
            item.name,
            "enum",
            item,
            fallback=fallback,
            children=_children_for_enum(item),
        ),)
    if isinstance(item, ast_nodes.TaggedUnionDecl):
        children = tuple(
            _make_symbol(
                variant.name,
                "struct",
                variant,
                children=_children_for_struct(variant.fields),
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
            symbol
            for port in item.ports
            for symbol in _symbols_for_port(port)
        )
        return (_make_symbol(
            item.name, "interface", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.ProtocolDecl):
        children = tuple(
            _make_symbol(channel.name, "field", channel)
            for channel in item.channels
        )
        return (_make_symbol(
            item.name, "interface", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.ResourceDefinitionDecl):
        children = tuple(
            _make_symbol(port.name, "field", port)
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
                children=tuple(
                    _make_symbol(field.name, "field", field)
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
                _make_symbol(state.member, "enum_member", state)
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
            _make_symbol(goal.name, "event", goal)
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
            module,
            fallback=module_origin,
            children=tuple(children),
        ),
    )


def _declaration_symbols(module: ast_nodes.Module) -> tuple[ToolingSymbol, ...]:
    """Project file-scope declarations independently of the physical modules."""

    items: tuple[object, ...] = (
        *module.type_aliases,
        *module.enums,
        *module.tagged_unions,
        *module.structs,
        *module.functions,
        *module.operators,
        *module.equivalences,
        *module.protocols,
        *module.resource_definitions,
        *module.target_families,
        *module.target_instances,
        *module.architecture_templates,
        *module.module_interfaces,
    )
    return _validated_symbols(tuple(
        symbol
        for item in items
        for symbol in _symbol_for_item(item, None)
    ))

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
        symbols = list(_declaration_symbols(syntax))
        symbols.extend(_module_symbols(syntax))
    symbols.extend(
        symbol for item in syntax.submodules for symbol in _module_symbols(item)
    )
    return _validated_symbols(tuple(symbols))
