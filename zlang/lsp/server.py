"""Minimal deterministic Community LSP server for ZLang HDL.

The server deliberately has no parser or semantic model of its own.  Every
source check goes through the Community tooling boundary; this module only owns
JSON-RPC framing, the small open-document state required by LSP, and the
mapping from compiler diagnostics and tooling projections to LSP values.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, BinaryIO, TextIO
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from zlang._version import __version__
from zlang.tooling import (
    ToolingDiagnostic,
    ToolingDefinition,
    ToolingError,
    ToolingHover,
    ToolingDiagnosticFix,
    ToolingReference,
    ToolingRenameError,
    ToolingSymbol,
    ToolingCompletion,
    ToolingSemanticToken,
    ToolingSignatureHelp,
    ToolingSession,
    completion_at,
    check_snapshot,
    document_symbols,
    definition_at,
    hover_at,
    is_unspecialized_generic_diagnostic,
    references_at,
    rename_at,
    semantic_tokens,
    signature_help_at,
)


SERVER_NAME = "zlang-lsp"
JSON_RPC_VERSION = "2.0"
TEXT_DOCUMENT_SYNC_FULL = 1
CODE_ACTION_QUICKFIX = "quickfix"

# LSP SymbolKind values, kept in one table so source categories do not leak
# numeric protocol constants through the compiler/tooling projection.
_SYMBOL_KINDS = {
    "module": 2,
    "namespace": 3,
    "class": 5,
    "method": 6,
    "field": 8,
    "enum": 10,
    "interface": 11,
    "function": 12,
    "variable": 13,
    "object": 19,
    "enum_member": 22,
    "struct": 23,
    "event": 24,
    "operator": 25,
    "type_parameter": 26,
    "type": 5,
}

_COMPLETION_KINDS = {
    "function": 3,
    "port": 5,
    "parameter": 6,
    "value": 6,
}

# The legend is intentionally static and contains only token classes emitted
# by the current compiler-owned tooling projection.
SEMANTIC_TOKEN_TYPES = ("parameter", "variable", "property", "function")
SEMANTIC_TOKEN_MODIFIERS = ("declaration",)
_SEMANTIC_TOKEN_TYPE_INDEX = {
    name: index for index, name in enumerate(SEMANTIC_TOKEN_TYPES)
}
_SEMANTIC_TOKEN_MODIFIER_INDEX = {
    name: index for index, name in enumerate(SEMANTIC_TOKEN_MODIFIERS)
}


class LspProtocolError(ValueError):
    """A malformed JSON-RPC/LSP message or unsupported local document URI."""


@dataclass(frozen=True)
class DocumentState:
    """The complete document state required by this first LSP slice."""

    uri: str
    version: int | None
    text: str
    path: Path
    navigation_top: str | None = None


def _request_position(params: object) -> tuple[int, int]:
    """Return one validated zero-based LSP position."""

    position = _mapping(params, "position")
    return _integer(position, "line"), _integer(position, "character")


def _utf16_units(value: str) -> int:
    """Return the LSP-default UTF-16 code-unit length of text."""

    return len(value.encode("utf-16-le")) // 2


def _compiler_character_to_lsp(
    source_text: str,
    line: int,
    column: int,
) -> int:
    """Convert one compiler code-point column to an LSP UTF-16 character."""

    lines = source_text.splitlines(keepends=True)
    line_index = line - 1
    if line_index < 0 or line_index >= len(lines):
        raise LspProtocolError("compiler source range is outside the document")
    line_text = lines[line_index].rstrip("\r\n")
    code_point_index = column - 1
    if code_point_index < 0 or code_point_index > len(line_text):
        raise LspProtocolError("compiler source range is outside the document")
    return _utf16_units(line_text[:code_point_index])


def _lsp_character_to_compiler(
    source_text: str,
    line: int,
    character: int,
) -> int:
    """Convert an LSP UTF-16 character offset to a code-point index."""

    lines = source_text.splitlines(keepends=True)
    if line < 0 or line >= len(lines):
        raise LspProtocolError("LSP position is outside the document")
    line_text = lines[line].rstrip("\r\n")
    if character < 0:
        raise LspProtocolError("LSP position is outside the document")
    units = 0
    for index, value in enumerate(line_text):
        if units == character:
            return index
        units += _utf16_units(value)
        if units > character:
            raise LspProtocolError(
                "LSP position splits a UTF-16 surrogate pair"
            )
    if units == character:
        return len(line_text)
    raise LspProtocolError("LSP position is outside the document")


def uri_to_path(uri: str) -> Path:
    """Convert a local ``file://`` URI to a path without guessing remotes."""

    if not isinstance(uri, str):
        raise LspProtocolError("document URI must be a string")
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "file":
        raise LspProtocolError(
            "zlang-lsp supports only local file:// document URIs"
        )
    if parsed.netloc not in {"", "localhost"}:
        raise LspProtocolError(
            "zlang-lsp does not support remote file-system URI authorities"
        )
    if not parsed.path:
        raise LspProtocolError("file URI has no local path")
    decoded = unquote(parsed.path)
    path = Path(url2pathname(decoded))
    if not path.is_absolute():
        raise LspProtocolError("file URI must identify an absolute local path")
    return path


def path_to_uri(path: Path | str) -> str:
    """Convert an absolute local path to a correctly escaped file URI."""

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = candidate.resolve()
    return candidate.as_uri()


def origin_to_range(
    origin: Any,
    source_text: str | None = None,
) -> dict[str, dict[str, int]]:
    """Convert a compiler/tooling origin to a zero-based LSP range.

    Compiler spans are one-based.  Missing origins are represented by the
    conventional zero-length range at the start of the document; the semantic
    diagnostic itself is still preserved in ``data``.
    """

    if origin is None:
        start_line = start_character = end_line = end_character = 0
    else:
        start_line = max(0, int(origin.start_line) - 1)
        end_line = max(0, int(origin.end_line) - 1)
        if source_text is None:
            start_character = max(0, int(origin.start_column) - 1)
            end_character = max(0, int(origin.end_column) - 1)
        else:
            start_character = _compiler_character_to_lsp(
                source_text,
                int(origin.start_line),
                int(origin.start_column),
            )
            end_character = _compiler_character_to_lsp(
                source_text,
                int(origin.end_line),
                int(origin.end_column),
            )
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


def diagnostic_to_lsp(
    diagnostic: ToolingDiagnostic,
    source_text: str | None = None,
) -> dict[str, Any]:
    """Map one compiler-owned tooling diagnostic without changing its meaning."""

    origin = diagnostic.primary
    return {
        "range": origin_to_range(origin, source_text),
        # Diagnostic currently represents compiler errors.  The tooling API
        # does not expose another severity, so this is the truthful mapping.
        "severity": 1,
        "code": diagnostic.code,
        "source": "zlang",
        "message": diagnostic.message,
        "data": {
            "source_unit": None if origin is None else origin.source_unit,
            "construct": None if origin is None else origin.construct,
            "notes": list(diagnostic.notes),
            "fixes": list(diagnostic.fixes),
        },
    }


def _lsp_range(value: object) -> dict[str, dict[str, int]]:
    """Validate and normalize one standard half-open LSP range."""

    item = _mapping(value)
    start = _mapping(item, "start")
    end = _mapping(item, "end")
    result = {
        "start": {
            "line": _integer(start, "line"),
            "character": _integer(start, "character"),
        },
        "end": {
            "line": _integer(end, "line"),
            "character": _integer(end, "character"),
        },
    }
    start_key = (result["start"]["line"], result["start"]["character"])
    end_key = (result["end"]["line"], result["end"]["character"])
    if end_key < start_key:
        raise LspProtocolError("LSP range end precedes its start")
    return result


def _ranges_intersect(
    first: dict[str, dict[str, int]],
    second: dict[str, dict[str, int]],
) -> bool:
    """Return whether two half-open LSP ranges overlap, including cursors."""

    first_start = (first["start"]["line"], first["start"]["character"])
    first_end = (first["end"]["line"], first["end"]["character"])
    second_start = (second["start"]["line"], second["start"]["character"])
    second_end = (second["end"]["line"], second["end"]["character"])
    if first_start == first_end:
        return second_start <= first_start < second_end
    if second_start == second_end:
        return first_start <= second_start < first_end
    return first_start < second_end and second_start < first_end


def _client_requested_diagnostic(
    current: dict[str, Any],
    requested: tuple[dict[str, Any], ...],
) -> bool:
    """Match stable diagnostic fields without interpreting their contents."""

    if not requested:
        return True
    return any(
        item.get("code") == current.get("code")
        and item.get("message") == current.get("message")
        and item.get("range") == current.get("range")
        for item in requested
    )


def _fix_to_code_action(
    fix: ToolingDiagnosticFix,
    diagnostic: dict[str, Any],
    state: DocumentState,
) -> dict[str, Any] | None:
    """Mechanically map one complete current-source fix to one CodeAction."""

    uri = state.uri
    edits: list[dict[str, Any]] = []
    for edit in fix.edits:
        if edit.source_path.resolve() != state.path.resolve():
            return None
        try:
            edit_range = origin_to_range(edit.origin, state.text)
        except LspProtocolError:
            return None
        edits.append({"range": edit_range, "newText": edit.replacement})
    return {
        "title": fix.title,
        "kind": CODE_ACTION_QUICKFIX,
        "diagnostics": [diagnostic],
        "edit": {"changes": {uri: edits}},
    }


def _symbol_to_lsp(symbol: ToolingSymbol) -> dict[str, Any]:
    """Map one compiler-owned symbol category to the standard LSP shape."""

    kind = _SYMBOL_KINDS.get(symbol.kind, _SYMBOL_KINDS["object"])
    result: dict[str, Any] = {
        "name": symbol.name,
        "kind": kind,
        "range": origin_to_range(symbol.range),
        "selectionRange": origin_to_range(symbol.selection_range),
    }
    if symbol.children:
        result["children"] = [_symbol_to_lsp(child) for child in symbol.children]
    return result


def _hover_to_lsp(hover: ToolingHover | None) -> dict[str, Any] | None:
    """Map one narrow compiler-owned hover projection to standard LSP."""

    if hover is None:
        return None
    lines: list[str] = []
    if hover.signature:
        lines.append(hover.signature)
    elif hover.name and hover.type_text:
        lines.append(f"{hover.name} : {hover.type_text}")
    elif hover.type_text:
        lines.append(hover.type_text)
    elif hover.name:
        lines.append(hover.name)
    details: list[str] = []
    if hover.port_direction:
        details.append(hover.port_direction)
    if hover.signedness:
        details.append(hover.signedness)
    if hover.width is not None:
        details.append(f"{hover.width} bit(s)")
    if hover.fixed_point:
        details.append(f"fixed-point {hover.fixed_point}")
    if details:
        lines.append(", ".join(details))
    result: dict[str, Any] = {
        "contents": {"kind": "markdown", "value": "\n".join(lines)},
    }
    if hover.origin is not None:
        result["range"] = origin_to_range(hover.origin)
    return result


def _definition_to_lsp(
    definition: ToolingDefinition | None,
) -> dict[str, Any] | None:
    if definition is None:
        return None
    return {
        "uri": path_to_uri(definition.target_path),
        "range": origin_to_range(definition.target_origin),
    }


def _reference_to_lsp(reference: ToolingReference) -> dict[str, Any]:
    return {
        "uri": path_to_uri(reference.source_path),
        "range": origin_to_range(reference.origin),
    }


def _completion_to_lsp(completion: ToolingCompletion) -> dict[str, Any]:
    """Map one compiler-visible candidate to a plain CompletionItem."""

    result: dict[str, Any] = {
        "label": completion.name,
        "kind": _COMPLETION_KINDS.get(completion.kind, 6),
        "insertText": completion.name,
    }
    if completion.detail is not None:
        result["detail"] = completion.detail
    return result


def _signature_help_to_lsp(
    signature: ToolingSignatureHelp | None,
) -> dict[str, Any] | None:
    """Map one compiler-resolved signature-help projection."""

    if signature is None:
        return None
    return {
        "signatures": [
            {
                "label": signature.label,
                "parameters": [
                    {"label": parameter}
                    for parameter in signature.parameters
                ],
            }
        ],
        "activeSignature": 0,
        "activeParameter": signature.active_parameter,
    }


def _semantic_token_position(
    token: ToolingSemanticToken,
    source_text: str,
) -> tuple[int, int, int, int, int] | None:
    """Convert one one-based compiler span to an absolute LSP token tuple."""

    origin = token.origin
    if origin.start_line != origin.end_line:
        return None
    lines = source_text.splitlines(keepends=True)
    line_index = origin.start_line - 1
    if line_index < 0 or line_index >= len(lines):
        return None
    line_text = lines[line_index].rstrip("\r\n")
    start_index = origin.start_column - 1
    end_index = origin.end_column - 1
    if (
        start_index < 0
        or end_index <= start_index
        or end_index > len(line_text)
    ):
        return None
    token_type = _SEMANTIC_TOKEN_TYPE_INDEX.get(token.kind)
    if token_type is None:
        return None
    modifier_bits = 0
    for modifier in token.modifiers:
        modifier_index = _SEMANTIC_TOKEN_MODIFIER_INDEX.get(modifier)
        if modifier_index is None:
            return None
        modifier_bits |= 1 << modifier_index
    start = _utf16_units(line_text[:start_index])
    length = _utf16_units(line_text[start_index:end_index])
    if length <= 0:
        return None
    return line_index, start, length, token_type, modifier_bits


def semantic_tokens_to_lsp(
    tokens: tuple[ToolingSemanticToken, ...],
    source_text: str,
) -> dict[str, list[int]]:
    """Sort and encode compiler-owned tokens using standard relative fields."""

    declaration_bit = 1 << _SEMANTIC_TOKEN_MODIFIER_INDEX["declaration"]
    absolute = sorted(
        (
            item
            for token in tokens
            if (item := _semantic_token_position(token, source_text)) is not None
        ),
        key=lambda item: (
            item[0],
            item[1],
            item[2],
            0 if item[4] & declaration_bit else 1,
            item[3],
            item[4],
        ),
    )
    non_overlapping: list[tuple[int, int, int, int, int]] = []
    for item in absolute:
        if non_overlapping:
            previous = non_overlapping[-1]
            if item[0] == previous[0] and item[1] < previous[1] + previous[2]:
                continue
        non_overlapping.append(item)

    data: list[int] = []
    previous_line = 0
    previous_start = 0
    for line, start, length, token_type, modifier_bits in non_overlapping:
        delta_line = line - previous_line
        delta_start = start - previous_start if delta_line == 0 else start
        data.extend((delta_line, delta_start, length, token_type, modifier_bits))
        previous_line = line
        previous_start = start
    return {"data": data}


def _server_diagnostic(code: str, message: str) -> dict[str, Any]:
    """Create an explicit protocol/environment diagnostic.

    These diagnostics are not semantic claims about ZLang.  They are used only
    when the LSP cannot invoke the existing compiler boundary for a document.
    """

    return {
        "range": origin_to_range(None),
        "severity": 1,
        "code": code,
        "source": SERVER_NAME,
        "message": message,
    }


def _publish(uri: str, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "jsonrpc": JSON_RPC_VERSION,
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": uri, "diagnostics": diagnostics},
    }


def _response(request_id: Any, result: Any = None) -> dict[str, Any]:
    return {"jsonrpc": JSON_RPC_VERSION, "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSON_RPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class LspServer:
    """Stateful dispatcher for the bounded Community LSP surface."""

    def __init__(self, *, log: TextIO | None = None) -> None:
        self.documents: dict[str, DocumentState] = {}
        self.tooling_session = ToolingSession()
        self._pending_navigation_tops: dict[Path, str] = {}
        self.shutdown_requested = False
        self.exit_requested = False
        self._log = log if log is not None else sys.stderr

    @property
    def capabilities(self) -> dict[str, Any]:
        """Only advertise capabilities implemented by this server."""

        return {
            "textDocumentSync": {
                "openClose": True,
                "change": TEXT_DOCUMENT_SYNC_FULL,
            },
            "documentSymbolProvider": True,
            "hoverProvider": True,
            "definitionProvider": True,
            "referencesProvider": True,
            "renameProvider": True,
            "completionProvider": {"resolveProvider": False},
            "signatureHelpProvider": {},
            "semanticTokensProvider": {
                "legend": {
                    "tokenTypes": list(SEMANTIC_TOKEN_TYPES),
                    "tokenModifiers": list(SEMANTIC_TOKEN_MODIFIERS),
                },
                "full": True,
                "range": False,
            },
            "codeActionProvider": {
                "codeActionKinds": [CODE_ACTION_QUICKFIX],
            },
        }

    def dispatch(self, message: object) -> list[dict[str, Any]]:
        """Dispatch one decoded JSON-RPC message and return outbound messages."""

        if not isinstance(message, dict):
            return [_error(None, -32600, "invalid JSON-RPC request")]
        method = message.get("method")
        has_id = "id" in message
        request_id = message.get("id")
        if not isinstance(method, str):
            return [_error(
                request_id if has_id else None, -32600, "method is required"
            )]
        params = message.get("params", {})
        if method == "initialize":
            if not has_id:
                return []
            return [_response(
                request_id,
                {
                    "capabilities": self.capabilities,
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                },
            )]
        if method == "initialized":
            return []
        if method == "shutdown":
            if not has_id:
                return []
            self.shutdown_requested = True
            return [_response(request_id, None)]
        if method == "exit":
            self.exit_requested = True
            return []
        if method == "textDocument/didOpen":
            return self._did_open(params)
        if method == "textDocument/didChange":
            return self._did_change(params)
        if method == "textDocument/didClose":
            return self._did_close(params)
        if method == "textDocument/documentSymbol":
            if not has_id:
                return []
            try:
                result = self._document_symbols(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            return [_response(request_id, result)]
        if method == "textDocument/hover":
            if not has_id:
                return []
            try:
                result = self._hover(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            except (ToolingError, OSError, ValueError) as error:
                return [_error(request_id, -32603, str(error))]
            return [_response(request_id, result)]
        if method == "textDocument/definition":
            if not has_id:
                return []
            try:
                result = self._definition(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            except (ToolingError, OSError, ValueError) as error:
                return [_error(request_id, -32603, str(error))]
            return [_response(request_id, result)]
        if method == "textDocument/references":
            if not has_id:
                return []
            try:
                result = self._references(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            except (ToolingError, OSError, ValueError) as error:
                return [_error(request_id, -32603, str(error))]
            return [_response(request_id, result)]
        if method == "textDocument/rename":
            if not has_id:
                return []
            try:
                result = self._rename(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            except ToolingRenameError as error:
                return [_error(request_id, -32602, str(error))]
            except (ToolingError, OSError, ValueError) as error:
                return [_error(request_id, -32603, str(error))]
            return [_response(request_id, result)]
        if method == "textDocument/completion":
            if not has_id:
                return []
            try:
                result = self._completion(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            except (ToolingError, OSError, ValueError) as error:
                return [_error(request_id, -32603, str(error))]
            return [_response(request_id, result)]
        if method == "textDocument/signatureHelp":
            if not has_id:
                return []
            try:
                result = self._signature_help(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            except (ToolingError, OSError, ValueError) as error:
                return [_error(request_id, -32603, str(error))]
            return [_response(request_id, result)]
        if method == "textDocument/semanticTokens/full":
            if not has_id:
                return []
            try:
                result = self._semantic_tokens_full(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            except (ToolingError, OSError, ValueError) as error:
                return [_error(request_id, -32603, str(error))]
            return [_response(request_id, result)]
        if method == "textDocument/codeAction":
            if not has_id:
                return []
            try:
                result = self._code_actions(params)
            except LspProtocolError as error:
                return [_error(request_id, -32602, str(error))]
            except (ToolingError, OSError, ValueError) as error:
                return [_error(request_id, -32603, str(error))]
            return [_response(request_id, result)]
        if has_id:
            return [_error(request_id, -32601, f"method not found: {method}")]
        return []

    def _open_document(self, params: object, operation: str) -> DocumentState:
        """Resolve one request to the exact in-memory document snapshot."""

        item = _mapping(params, "textDocument")
        uri = _string(item, "uri")
        path = uri_to_path(uri)
        state = self.documents.get(uri)
        if state is None:
            raise LspProtocolError(
                f"{operation} requested for a document that is not open"
            )
        if path != state.path:
            raise LspProtocolError("document URI changed while it was open")
        return state

    def _document_symbols(self, params: object) -> list[dict[str, Any]]:
        state = self._open_document(params, "documentSymbol")
        return [_symbol_to_lsp(symbol) for symbol in document_symbols(state.text)]

    def _hover(self, params: object) -> dict[str, Any] | None:
        state = self._open_document(params, "hover")
        line, character = _request_position(params)
        return _hover_to_lsp(
            hover_at(
                state.path,
                state.text,
                line,
                character,
                _session=self.tooling_session,
            )
        )

    def _definition(self, params: object) -> dict[str, Any] | None:
        state = self._open_document(params, "definition")
        line, character = _request_position(params)
        compiler_character = _lsp_character_to_compiler(
            state.text, line, character
        )
        definition = definition_at(
            state.path,
            state.text,
            line,
            compiler_character,
            _session=self.tooling_session,
        )
        if definition is not None and definition.kind == "module":
            self._pending_navigation_tops[
                definition.target_path.resolve()
            ] = definition.name
        return _definition_to_lsp(definition)

    def _references(self, params: object) -> list[dict[str, Any]]:
        state = self._open_document(params, "references")
        line, character = _request_position(params)
        compiler_character = _lsp_character_to_compiler(
            state.text, line, character
        )
        context = params.get("context") if isinstance(params, dict) else None
        include_declaration = False
        if context is not None:
            context_object = _mapping(context)
            candidate = context_object.get("includeDeclaration", False)
            if not isinstance(candidate, bool):
                raise LspProtocolError(
                    "reference context includeDeclaration must be a boolean"
                )
            include_declaration = candidate
        return [
            _reference_to_lsp(reference)
            for reference in references_at(
                state.path,
                state.text,
                line,
                compiler_character,
                include_declaration,
                _session=self.tooling_session,
            )
        ]

    def _rename(self, params: object) -> dict[str, Any] | None:
        state = self._open_document(params, "rename")
        line, character = _request_position(params)
        new_name = _string(params, "newName")
        edits = rename_at(
            state.path,
            state.text,
            line,
            character,
            new_name,
            _session=self.tooling_session,
        )
        if edits is None:
            return None
        changes: dict[str, list[dict[str, Any]]] = {}
        for edit in edits:
            changes.setdefault(path_to_uri(edit.source_path), []).append(
                {
                    "range": origin_to_range(edit.origin),
                    "newText": edit.new_text,
                }
            )
        return {"changes": changes}

    def _completion(self, params: object) -> list[dict[str, Any]]:
        state = self._open_document(params, "completion")
        line, character = _request_position(params)
        return [
            _completion_to_lsp(item)
            for item in completion_at(
                state.path,
                state.text,
                line,
                character,
                _session=self.tooling_session,
            )
        ]

    def _signature_help(self, params: object) -> dict[str, Any] | None:
        state = self._open_document(params, "signatureHelp")
        line, character = _request_position(params)
        return _signature_help_to_lsp(
            signature_help_at(
                state.path,
                state.text,
                line,
                character,
                _session=self.tooling_session,
            )
        )

    def _semantic_tokens_full(self, params: object) -> dict[str, list[int]]:
        state = self._open_document(params, "semanticTokens/full")
        return semantic_tokens_to_lsp(
            semantic_tokens(
                state.path,
                state.text,
                _session=self.tooling_session,
                _top=state.navigation_top,
            ),
            state.text,
        )

    def _code_actions(self, params: object) -> list[dict[str, Any]]:
        state = self._open_document(params, "codeAction")
        requested_range = _lsp_range(
            params.get("range") if isinstance(params, dict) else None
        )
        context = _mapping(params, "context")
        client_diagnostics = context.get("diagnostics")
        if not isinstance(client_diagnostics, list) or any(
            not isinstance(diagnostic, dict)
            for diagnostic in client_diagnostics
        ):
            raise LspProtocolError(
                "codeAction context diagnostics must be an array"
            )
        requested_diagnostics = tuple(client_diagnostics)
        only = context.get("only")
        if only is not None:
            if not isinstance(only, list) or any(
                not isinstance(kind, str) for kind in only
            ):
                raise LspProtocolError(
                    "codeAction context only must be a string array"
                )
            if CODE_ACTION_QUICKFIX not in only:
                return []

        digest = hashlib.sha256(state.text.encode("utf-8")).hexdigest()
        record = check_snapshot(
            state.path,
            state.text,
            source_digest=digest,
            _session=self.tooling_session,
        )
        actions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tooling_diagnostic in record.diagnostics:
            current = diagnostic_to_lsp(tooling_diagnostic, state.text)
            if not _ranges_intersect(requested_range, current["range"]):
                continue
            if not _client_requested_diagnostic(
                current,
                requested_diagnostics,
            ):
                continue
            for fix in tooling_diagnostic.machine_fixes:
                action = _fix_to_code_action(fix, current, state)
                if action is None:
                    continue
                identity = json.dumps(
                    action,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                actions.append(action)
        actions.sort(
            key=lambda action: (
                action["diagnostics"][0]["range"]["start"]["line"],
                action["diagnostics"][0]["range"]["start"]["character"],
                action["title"],
                json.dumps(action["edit"], sort_keys=True),
            )
        )
        return actions

    def _did_open(self, params: object) -> list[dict[str, Any]]:
        try:
            item = _mapping(params, "textDocument")
            uri = _string(item, "uri")
            path = uri_to_path(uri)
            version = _optional_version(item.get("version"))
            text = _string(item, "text")
        except LspProtocolError as error:
            uri = _best_effort_uri(params)
            return [_publish(uri, [_server_diagnostic("ZL-LSP-URI-001", str(error))])]
        previous = self.documents.get(uri)
        if previous is not None:
            self.tooling_session.invalidate(previous.path)
        state = DocumentState(
            uri,
            version,
            text,
            path,
            self._pending_navigation_tops.pop(path.resolve(), None),
        )
        self.documents[uri] = state
        return [self._publish_document(state)]

    def _did_change(self, params: object) -> list[dict[str, Any]]:
        try:
            item = _mapping(params, "textDocument")
            uri = _string(item, "uri")
            version = _optional_version(item.get("version"))
            changes = params.get("contentChanges") if isinstance(params, dict) else None
            if not isinstance(changes, list) or len(changes) != 1:
                raise LspProtocolError(
                    "zlang-lsp full synchronization requires one content change"
                )
            change = changes[0]
            if not isinstance(change, dict) or "range" in change:
                raise LspProtocolError(
                    "zlang-lsp supports full-text changes only"
                )
            text = _string(change, "text")
        except LspProtocolError as error:
            uri = _best_effort_uri(params)
            return [_publish(
                uri, [_server_diagnostic("ZL-LSP-CHANGE-001", str(error))]
            )]
        current = self.documents.get(uri)
        if current is None:
            return [_publish(uri, [_server_diagnostic(
                "ZL-LSP-CHANGE-002", "received a change for a document that is not open"
            )])]
        if (
            version is not None
            and current.version is not None
            and version < current.version
        ):
            return []
        self.tooling_session.invalidate(current.path)
        state = DocumentState(uri, version, text, current.path)
        self.documents[uri] = state
        return [self._publish_document(state)]

    def _did_close(self, params: object) -> list[dict[str, Any]]:
        try:
            item = _mapping(params, "textDocument")
            uri = _string(item, "uri")
        except LspProtocolError as error:
            return [_publish("", [_server_diagnostic("ZL-LSP-CLOSE-001", str(error))])]
        self.documents.pop(uri, None)
        # Closing a VS Code preview removes only the editor-owned buffer.  Keep
        # the bounded semantic/symbol LRU entry: its key includes the exact
        # text digest and every reuse revalidates the source/dependency
        # environment.  Invalidating here made F12 preview navigation compile
        # the same unchanged mapper/IFFT file on every visit.
        return [_publish(uri, [])]

    def _publish_document(self, state: DocumentState) -> dict[str, Any]:
        digest = hashlib.sha256(state.text.encode("utf-8")).hexdigest()
        try:
            # A definition-aware shard exists only after successful semantic
            # analysis.  If an exact saved child was already observed while
            # compiling the source that navigated here, it is sufficient proof
            # of an empty current diagnostic set and avoids a redundant child
            # compile on VS Code preview open.
            symbol_proof = self.tooling_session.symbol_snapshot(
                state.path, state.text
            )
            if symbol_proof is None and state.navigation_top is not None:
                symbol_proof = self.tooling_session.symbol_snapshot_covering(
                    state.path,
                    state.text,
                    required_module=state.navigation_top,
                )
            if symbol_proof is not None:
                return _publish(state.uri, [])
            record = check_snapshot(
                state.path,
                state.text,
                source_digest=digest,
                _session=self.tooling_session,
            )
        except ToolingError as error:
            return _publish(
                state.uri,
                [_server_diagnostic("ZL-LSP-CHECK-001", str(error))],
            )
        except (OSError, ValueError) as error:
            # Path/project failures are explicit environment limitations.  Do
            # not present them as a successful semantic check.
            return _publish(
                state.uri,
                [_server_diagnostic("ZL-LSP-CHECK-002", str(error))],
            )
        except Exception as error:  # pragma: no cover - defensive process boundary
            print(
                f"{SERVER_NAME}: compiler integration failure: {type(error).__name__}",
                file=self._log,
            )
            return _publish(state.uri, [_server_diagnostic(
                "ZL-LSP-CHECK-003",
                "compiler integration failed; see the language-server log",
            )])
        return _publish(
            state.uri,
            [
                diagnostic_to_lsp(item, state.text)
                for item in record.diagnostics
                # Generic module declarations are checked for each concrete
                # specialization.  Opening the unspecialized library source is
                # an editor navigation action, not an invalid hardware build.
                if not is_unspecialized_generic_diagnostic(state.text, item)
            ],
        )

    def run(self, input_stream: BinaryIO, output_stream: BinaryIO) -> int:
        """Run the standard Content-Length framed JSON-RPC loop."""

        while not self.exit_requested:
            try:
                message = read_message(input_stream)
            except LspProtocolError as error:
                write_message(output_stream, _error(None, -32700, str(error)))
                break
            if message is None:
                break
            for outbound in self.dispatch(message):
                write_message(output_stream, outbound)
        return 0 if self.shutdown_requested else 1


def _mapping(value: object, key: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LspProtocolError("LSP parameters must be an object")
    if key is None:
        return value
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise LspProtocolError(f"LSP parameters are missing object '{key}'")
    return nested


def _string(value: object, key: str) -> str:
    if not isinstance(value, dict) or not isinstance(value.get(key), str):
        raise LspProtocolError(f"LSP field '{key}' must be a string")
    return value[key]


def _optional_version(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise LspProtocolError("document version must be an integer")
    return value


def _integer(value: object, key: str) -> int:
    if not isinstance(value, dict):
        raise LspProtocolError("LSP position must be an object")
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise LspProtocolError(f"LSP position field '{key}' must be an integer")
    if candidate < 0:
        raise LspProtocolError(f"LSP position field '{key}' must not be negative")
    return candidate


def _best_effort_uri(params: object) -> str:
    if isinstance(params, dict):
        item = params.get("textDocument")
        if isinstance(item, dict) and isinstance(item.get("uri"), str):
            return item["uri"]
    return ""


def read_message(stream: BinaryIO) -> object | None:
    """Read one standard LSP message from a binary stream."""

    first = stream.readline()
    if first == b"":
        return None
    headers: dict[str, str] = {}
    line = first
    while line not in {b"\r\n", b"\n"}:
        try:
            name, value = line.decode("ascii").rstrip("\r\n").split(":", 1)
        except (UnicodeDecodeError, ValueError) as error:
            raise LspProtocolError("malformed LSP header") from error
        headers[name.strip().lower()] = value.strip()
        line = stream.readline()
        if line == b"":
            raise LspProtocolError("truncated LSP headers")
    value = headers.get("content-length")
    if value is None:
        raise LspProtocolError("LSP message is missing Content-Length")
    try:
        length = int(value)
    except ValueError as error:
        raise LspProtocolError("LSP Content-Length is not an integer") from error
    if length < 0:
        raise LspProtocolError("LSP Content-Length must not be negative")
    payload = stream.read(length)
    if len(payload) != length:
        raise LspProtocolError("truncated LSP message body")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LspProtocolError("invalid JSON-RPC message") from error


def write_message(stream: BinaryIO, message: object) -> None:
    payload = json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    stream.write(payload)
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


def run_server() -> int:
    """Console-script entry point for ``zlang-lsp``."""

    return LspServer().run(sys.stdin.buffer, sys.stdout.buffer)


def main() -> int:
    return run_server()


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())


__all__ = [
    "DocumentState",
    "LspProtocolError",
    "LspServer",
    "SEMANTIC_TOKEN_MODIFIERS",
    "SEMANTIC_TOKEN_TYPES",
    "diagnostic_to_lsp",
    "main",
    "origin_to_range",
    "path_to_uri",
    "read_message",
    "run_server",
    "semantic_tokens_to_lsp",
    "uri_to_path",
    "write_message",
]
