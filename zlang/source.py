"""Backend-independent source locations retained through compiler IR stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import total_ordering
import re
from typing import Mapping


@dataclass(frozen=True, order=True)
class SourceSpan:
    """One half-open source range using one-based lines and columns."""

    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        if min(
            self.start_line,
            self.start_column,
            self.end_line,
            self.end_column,
        ) < 1:
            raise ValueError("source coordinates must be positive")
        if (self.end_line, self.end_column) < (
            self.start_line,
            self.start_column,
        ):
            raise ValueError("source span end precedes its start")

    def render(self) -> str:
        return (
            f"{self.start_line}:{self.start_column}-"
            f"{self.end_line}:{self.end_column}"
        )

    def to_data(self) -> dict[str, int]:
        """Return the deterministic, JSON-compatible span representation."""
        return {
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> "SourceSpan":
        """Restore a span from its JSON-compatible representation."""
        if not isinstance(data, Mapping):
            raise ValueError("source span must be an object")
        try:
            coordinates = tuple(
                data[name]
                for name in ("start_line", "start_column", "end_line", "end_column")
            )
        except KeyError as error:
            raise ValueError(f"source span is missing {error.args[0]}") from error
        if any(isinstance(value, bool) or not isinstance(value, int) for value in coordinates):
            raise ValueError("source coordinates must be integers")
        return cls(*coordinates)


@total_ordering
@dataclass(frozen=True)
class SourceOrigin:
    """A source span plus the source-level construct it denotes."""

    span: SourceSpan
    construct: str
    # These fields are serialized explicitly, but excluded from ``repr`` so
    # legacy identity code that historically embedded ``repr(SourceOrigin)``
    # cannot accidentally turn provenance into value/implementation semantics.
    source_unit: str | None = field(default=None, repr=False)
    digest: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.construct:
            raise ValueError("source-origin construct must not be empty")
        if self.source_unit is not None and not self.source_unit:
            raise ValueError("source-origin source unit must not be empty")
        if self.digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("source-origin digest must be a lowercase SHA-256 hex digest")

    def render(self) -> str:
        """Render the historical diagnostic spelling.

        Logical source identity deliberately stays out of this spelling so old
        exception strings, reports, and positional construction remain stable.
        """
        return f"{self.span.render()}:{self.construct}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SourceOrigin):
            return NotImplemented
        return (
            self.span,
            self.construct,
            self.source_unit or "",
            self.digest or "",
        ) < (
            other.span,
            other.construct,
            other.source_unit or "",
            other.digest or "",
        )

    def to_data(self) -> dict[str, object]:
        """Return the deterministic, JSON-compatible structured origin."""
        return {
            "construct": self.construct,
            "digest": self.digest,
            "source_unit": self.source_unit,
            "span": self.span.to_data(),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, object] | str) -> "SourceOrigin":
        """Restore a structured origin, accepting legacy rendered strings.

        Rendered strings cannot contain the source unit or digest, but accepting
        them keeps old BackendArtifact manifests readable.
        """
        if isinstance(data, str):
            match = re.fullmatch(r"(\d+):(\d+)-(\d+):(\d+):(.+)", data)
            if match is None:
                raise ValueError("invalid rendered source origin")
            return cls(
                SourceSpan(*(int(value) for value in match.groups()[:4])),
                match.group(5),
            )
        if not isinstance(data, Mapping):
            raise ValueError("source origin must be an object or rendered string")
        try:
            span = SourceSpan.from_data(data["span"])
            construct = data["construct"]
        except KeyError as error:
            raise ValueError(f"source origin is missing {error.args[0]}") from error
        if not isinstance(construct, str):
            raise ValueError("source-origin construct must be a string")
        source_unit = data.get("source_unit")
        digest = data.get("digest")
        if source_unit is not None and not isinstance(source_unit, str):
            raise ValueError("source-origin source unit must be a string")
        if digest is not None and not isinstance(digest, str):
            raise ValueError("source-origin digest must be a string")
        return cls(span, construct, source_unit, digest)
