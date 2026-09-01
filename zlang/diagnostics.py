"""Stable, backend-independent compiler diagnostics.

The public exception classes in ZLang historically exposed only their message.
``DiagnosticError`` deliberately preserves that API: ``str(error)`` is exactly
the message passed to the constructor, while structured consumers can inspect
the attached :class:`Diagnostic` or request deterministic JSON from ``zlangc``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping

from zlang.source import SourceOrigin


DIAGNOSTIC_SCHEMA = "zlang-diagnostic-v1"


@dataclass(frozen=True)
class Diagnostic:
    """One stable compiler diagnostic independent of its presentation."""

    code: str
    message: str
    primary: SourceOrigin | None = None
    notes: tuple[str, ...] = ()
    fixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("diagnostic code must not be empty")
        if not self.message:
            raise ValueError("diagnostic message must not be empty")
        if any(not item for item in (*self.notes, *self.fixes)):
            raise ValueError("diagnostic notes and fixes must not be empty")

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned public JSON representation."""

        return {
            "schema": DIAGNOSTIC_SCHEMA,
            "severity": "error",
            "code": self.code,
            "message": self.message,
            "primary": (
                None if self.primary is None else self.primary.to_data()
            ),
            "notes": list(self.notes),
            "fixes": list(self.fixes),
        }

    def to_json(self) -> str:
        """Render deterministic compact JSON suitable for one CLI line."""

        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Diagnostic":
        """Validate and restore the versioned public representation."""

        if data.get("schema") != DIAGNOSTIC_SCHEMA:
            raise ValueError("unsupported diagnostic schema")
        if data.get("severity") != "error":
            raise ValueError("diagnostic severity must be 'error'")
        code = data.get("code")
        message = data.get("message")
        primary_data = data.get("primary")
        notes = data.get("notes")
        fixes = data.get("fixes")
        if not isinstance(code, str) or not isinstance(message, str):
            raise ValueError("diagnostic code and message must be strings")
        if primary_data is not None and not isinstance(primary_data, Mapping):
            raise ValueError("diagnostic primary origin must be an object or null")
        if (
            not isinstance(notes, list)
            or any(not isinstance(item, str) for item in notes)
            or not isinstance(fixes, list)
            or any(not isinstance(item, str) for item in fixes)
        ):
            raise ValueError("diagnostic notes and fixes must be string arrays")
        return cls(
            code,
            message,
            (
                None
                if primary_data is None
                else SourceOrigin.from_data(primary_data)
            ),
            tuple(notes),
            tuple(fixes),
        )

    @classmethod
    def from_json(cls, payload: str) -> "Diagnostic":
        """Restore one diagnostic from JSON."""

        data = json.loads(payload)
        if not isinstance(data, Mapping):
            raise ValueError("diagnostic JSON must contain an object")
        return cls.from_dict(data)


class DiagnosticError(ValueError):
    """A legacy-compatible exception carrying one structured diagnostic."""

    default_code = "ZL-ERROR-001"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        primary: SourceOrigin | None = None,
        notes: Iterable[str] = (),
        fixes: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.diagnostic = Diagnostic(
            code or self.default_code,
            message,
            primary,
            tuple(notes),
            tuple(fixes),
        )

    @property
    def code(self) -> str:
        return self.diagnostic.code

    @property
    def primary(self) -> SourceOrigin | None:
        return self.diagnostic.primary

    @property
    def notes(self) -> tuple[str, ...]:
        return self.diagnostic.notes

    @property
    def fixes(self) -> tuple[str, ...]:
        return self.diagnostic.fixes


def diagnostic_from_exception(
    error: BaseException,
    *,
    code: str = "ZL-ERROR-001",
) -> Diagnostic:
    """Return structured data for typed and still-legacy exceptions."""

    if isinstance(error, DiagnosticError):
        return error.diagnostic
    return Diagnostic(code, str(error))


__all__ = [
    "DIAGNOSTIC_SCHEMA",
    "Diagnostic",
    "DiagnosticError",
    "diagnostic_from_exception",
]
