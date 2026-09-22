"""Fail-closed source-span projection for parser-proven trivia-only edits."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import json

from lark.exceptions import UnexpectedInput

from zlang.parser import ParseError, parse, significant_tokens
from zlang.source import SourceOrigin, SourceSpan

_MAX_TRIVIA_TOKENS = 16_384


def _lines(text: str) -> tuple[int, ...]:
    return (0, *(index + 1 for index, char in enumerate(text) if char == "\n"))


def _offset(text: str, lines: tuple[int, ...], line: int, column: int) -> int | None:
    if line < 1 or line > len(lines) or column < 1:
        return None
    result = lines[line - 1] + column - 1
    limit = lines[line] - 1 if line < len(lines) else len(text)
    if result > limit:
        return None
    return result


def _position(lines: tuple[int, ...], offset: int) -> tuple[int, int]:
    line = bisect_right(lines, offset) - 1
    return line + 1, offset - lines[line] + 1


@dataclass(frozen=True)
class TriviaRebinding:
    """One verified mapping of token-anchored spans to a new source snapshot."""

    old_text: str
    new_text: str
    anchors: tuple[tuple[int, int, int, int], ...]
    new_digest: str
    syntax_identity: str
    old_lines: tuple[int, ...]
    new_lines: tuple[int, ...]
    token_starts: tuple[int, ...]
    token_ends: tuple[int, ...]

    @classmethod
    def between(cls, old_text: str, new_text: str) -> "TriviaRebinding | None":
        if "\r" in old_text or "\r" in new_text:
            return None
        try:
            old = significant_tokens(old_text)
            new = significant_tokens(new_text)
            # Token types/values alone are insufficient in a contextual
            # grammar; both complete parses must have the same AST structure.
            if (
                len(old) > _MAX_TRIVIA_TOKENS
                or len(old) != len(new)
                or any(left[:2] != right[:2] for left, right in zip(old, new))
                or parse(old_text) != parse(new_text)
            ):
                return None
        except (ParseError, UnexpectedInput, ValueError):
            return None
        old_lines = _lines(old_text)
        new_lines = _lines(new_text)
        anchors = []
        for left, right in zip(old, new):
            a = _offset(old_text, old_lines, left[2], left[3])
            b = _offset(old_text, old_lines, left[4], left[5])
            c = _offset(new_text, new_lines, right[2], right[3])
            d = _offset(new_text, new_lines, right[4], right[5])
            if None in (a, b, c, d) or b - a != d - c:
                return None
            anchors.append((a, b, c, d))
        return cls(
            old_text,
            new_text,
            tuple(anchors),
            hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
            hashlib.sha256(
                json.dumps(
                    {
                        "schema": "zlang-significant-syntax-v1",
                        "tokens": [item[:2] for item in new],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest(),
            old_lines,
            new_lines,
            tuple(item[0] for item in anchors),
            tuple(item[1] for item in anchors),
        )

    def position(
        self, line: int, column: int, *, end: bool = False
    ) -> tuple[int, int] | None:
        old_offset = _offset(self.old_text, self.old_lines, line, column)
        if old_offset is None:
            return None
        # Adjacent old tokens may separate in the new text.  At their shared
        # boundary a span start belongs to the next token, a span end to the
        # preceding token; choosing either arbitrarily yields stale F12 spans.
        values = self.token_ends if end else self.token_starts
        index = bisect_left(values, old_offset)
        if index < len(values) and values[index] == old_offset:
            return _position(self.new_lines, self.anchors[index][3 if end else 2])
        index = bisect_right(self.token_starts, old_offset) - 1
        if index >= 0:
            old_start, old_end, new_start, _ = self.anchors[index]
            if old_start < old_offset < old_end:
                return _position(self.new_lines, new_start + old_offset - old_start)
        return None

    def origin(self, origin: SourceOrigin) -> SourceOrigin | None:
        start = self.position(origin.span.start_line, origin.span.start_column)
        end = self.position(origin.span.end_line, origin.span.end_column, end=True)
        if start is None or end is None:
            return None
        return SourceOrigin(
            SourceSpan(*start, *end),
            origin.construct,
            origin.source_unit,
            self.new_digest,
        )
