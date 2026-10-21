"""Exact compiler/code-point to LSP/UTF-16 position conversion."""

from typing import Any

from zlang.lsp.protocol import LspProtocolError


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _line(source_text: str, line: int, error: str) -> str:
    lines = source_text.splitlines(keepends=True)
    # splitlines omits the empty document and the empty line following a final
    # newline. Both have a valid zero-length EOF position, not a missing line.
    if not lines or source_text.endswith(("\n", "\r")):
        lines.append("")
    if line < 0 or line >= len(lines):
        raise LspProtocolError(error)
    return lines[line].rstrip("\r\n")


def compiler_character_to_lsp(source_text: str, line: int, column: int) -> int:
    error = "compiler source range is outside the document"
    text = _line(source_text, line - 1, error)
    if column < 1 or column > len(text) + 1:
        raise LspProtocolError(error)
    return utf16_units(text[:column - 1])


def lsp_character_to_compiler(source_text: str, line: int, character: int) -> int:
    text = _line(source_text, line, "LSP position is outside the document")
    if character < 0:
        raise LspProtocolError("LSP position is outside the document")
    units = 0
    for index, value in enumerate(text):
        if units == character:
            return index
        units += utf16_units(value)
        if units > character:
            raise LspProtocolError("LSP position splits a UTF-16 surrogate pair")
    if units == character:
        return len(text)
    raise LspProtocolError("LSP position is outside the document")


def origin_to_range(origin: Any, source_text: str | None = None) -> dict:
    """Map an exact one-based compiler span, without clamping stale ranges."""
    if origin is None:
        return {"start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 0}}
    result = {}
    for name in ("start", "end"):
        line, column = getattr(origin, f"{name}_line"), getattr(origin, f"{name}_column")
        character = (
            compiler_character_to_lsp(source_text, int(line), int(column))
            if source_text is not None else max(0, int(column) - 1)
        )
        result[name] = {"line": max(0, int(line) - 1), "character": character}
    return result
