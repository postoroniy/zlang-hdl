"""Diagnostic source binding and per-open-root publication ownership."""

import hashlib
from pathlib import Path
from typing import Any

from zlang.lsp.positions import origin_to_range
from zlang.tooling_models import ToolingError
from zlang.tooling_session import ToolingSession
from zlang.tooling_symbols import ToolingDiagnostic


def diagnostic_to_lsp(diagnostic: ToolingDiagnostic, source_text: str | None = None) -> dict:
    origin = diagnostic.primary
    return {
        "range": origin_to_range(origin, source_text),
        "severity": 1 if diagnostic.severity == "error" else 2,
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


def diagnostic_source(
    diagnostic: ToolingDiagnostic, source: Path, session: ToolingSession,
) -> tuple[Path, str]:
    """Resolve through compiler metadata, never reinterpret an import as root."""
    unit = diagnostic.primary.source_unit if diagnostic.primary is not None else None
    path = diagnostic.source_path
    if path is None:
        path = source if unit is None else session.source_path_for_unit(source, unit)
    if path is None:
        raise ToolingError(f"cannot locate diagnostic source '{unit}': {diagnostic.message}")
    path = path.resolve()
    text = session.editor_text_for(path)
    if text is None:
        # Decode bytes exactly as the resolver does: read_text would normalize
        # CRLF and invalidate the compiler's exact source digest.
        text = path.read_bytes().decode("utf-8")
    if diagnostic.source_digest is not None and hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest() != diagnostic.source_digest:
        raise ToolingError(f"diagnostic source changed during checking: {path}; recheck required")
    return path, text


class DiagnosticPublications:
    """Own contributions from open roots, including their closed imports.

    Replacing/closing a root clears its retired markers, without clearing an
    error still reported by another open root. No source samples are retained.
    """

    def __init__(self) -> None:
        self._roots: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def replace(self, root: str, groups: dict[str, list[dict]]) -> dict[str, list[dict]]:
        previous = self._roots.pop(root, {})
        if groups:
            self._roots[root] = groups
        result = {}
        for uri in sorted({root, *previous, *groups}, key=lambda uri: (uri != root, uri)):
            combined = []
            for owner in sorted(self._roots):
                for item in self._roots[owner].get(uri, ()):
                    if item not in combined:
                        combined.append(item)
            result[uri] = combined
        return result
