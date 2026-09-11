"""External Verilator orchestration for generated hardware."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

from zlang.backend.source_map import GeneratedSourceMap


class ToolchainError(RuntimeError):
    """An optional external compiler is missing or rejected generated code."""


_GENERATED_LOCATION = re.compile(
    r"(?P<path>[^\s:]+\.(?:hs|sv|v)):(?P<line>[1-9][0-9]*)(?::[1-9][0-9]*)?"
)


@dataclass(frozen=True)
class GeneratedDiagnosticContext:
    """One hash-verified source map embedded in a combined formal source."""

    source_map: GeneratedSourceMap
    generated_text: str
    line_offset: int = 0

    def __post_init__(self) -> None:
        if self.line_offset < 0:
            raise ValueError("generated diagnostic line offset must be non-negative")
        if hashlib.sha256(self.generated_text.encode()).hexdigest() != (
            self.source_map.artifact_hash
        ):
            raise ValueError(
                "generated diagnostic text does not match its source-map hash"
            )


def attribute_combined_generated_diagnostic(
    detail: str,
    contexts: tuple[GeneratedDiagnosticContext, ...],
) -> str:
    """Append one exact ZLang origin for a combined formal source diagnostic.

    SBY feeds implementation and checker text to Yosys as one generated file.
    Each source map still authenticates only its original backend artifact, so
    ``line_offset`` translates the combined-file line back to that artifact
    without weakening the map's hash boundary.
    """

    matches: list[object] = []
    for generated_location in _GENERATED_LOCATION.finditer(detail):
        combined_line = int(generated_location.group("line"))
        for context in contexts:
            local_line = combined_line - context.line_offset
            if local_line < 1:
                continue
            matches.extend(context.source_map.entries_for_line(local_line))
    unique = {
        (
            item.semantic_identity,
            item.source_origin.render(),
        ): item
        for item in matches
    }
    if len(unique) != 1:
        return detail
    entry = next(iter(unique.values()))
    origin = entry.source_origin
    unit = origin.source_unit or "<unknown-source>"
    return (
        f"{detail}\nZLang origin: {unit}:{origin.span.render()} "
        f"({origin.construct})"
    )


def attribute_generated_diagnostic(
    detail: str,
    source_map: GeneratedSourceMap | None,
    generated_text: str | None,
) -> str:
    """Append an exact ZLang origin when a verified map covers the tool line.

    Attribution is deliberately fail closed: a missing text payload, a changed
    artifact hash, an unrecognized tool location, or an unmapped line returns
    the diagnostic unchanged.
    """

    if source_map is None or generated_text is None:
        return detail
    if hashlib.sha256(generated_text.encode()).hexdigest() != source_map.artifact_hash:
        return detail
    return attribute_combined_generated_diagnostic(
        detail,
        (GeneratedDiagnosticContext(source_map, generated_text),),
    )


def lint_with_verilator(
    verilog_files: tuple[Path, ...],
    top_module: str,
    verilator_executable: str | None = None,
    source_map: GeneratedSourceMap | None = None,
) -> None:
    """Run Verilator's synthesizable RTL lint over generated Verilog."""

    executable = verilator_executable or shutil.which("verilator")
    if executable is None:
        raise ToolchainError("Verilator executable was not found")
    completed = subprocess.run(
        [
            executable,
            "--lint-only",
            "--top-module",
            top_module,
            *(str(path) for path in verilog_files),
        ],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        generated_text = None
        if source_map is not None:
            for path in verilog_files:
                try:
                    candidate = path.read_text()
                except OSError:
                    continue
                if hashlib.sha256(candidate.encode()).hexdigest() == source_map.artifact_hash:
                    generated_text = candidate
                    break
        detail = attribute_generated_diagnostic(
            detail, source_map, generated_text
        )
        raise ToolchainError(f"Verilator lint failed: {detail}")
