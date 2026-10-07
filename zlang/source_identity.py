"""Public product identity and canonical physical source-file policy.

Logical module names and the internal :mod:`zlang` Python namespace remain
extension-independent.  This module is deliberately small so filesystem,
packaging, editor, and publication code cannot drift onto different source
suffixes.
"""

from __future__ import annotations

from pathlib import Path


PUBLIC_LANGUAGE_NAME = "ZLang HDL"
SHORT_LANGUAGE_NAME = "ZLang"
DISTRIBUTION_NAME = "zlang-hdl"
CLI_NAME = "zlang"
SOURCE_SUFFIX = ".zhl"
SOURCE_GLOB = f"*{SOURCE_SUFFIX}"
LEGACY_SOURCE_SUFFIXES = frozenset({".zl", ".zlang"})
VSCODE_LANGUAGE_ID = "zlang-hdl"
MIME_TYPE = "text/x-zlang-hdl"


class SourceExtensionError(ValueError):
    """A physical compiler input does not use the canonical source suffix."""


def validate_source_path(path: Path | str) -> Path:
    """Return *path* when it is a canonical ZLang HDL source.

    In-memory ``compile_source`` callers remain filename-independent.  Every
    filesystem-backed entry point uses this check before reading bytes, so an
    unrelated ``.zl`` file is never interpreted as ZLang HDL by accident.
    """

    candidate = Path(path)
    if candidate.suffix == SOURCE_SUFFIX:
        return candidate
    if candidate.suffix in LEGACY_SOURCE_SUFFIXES:
        raise SourceExtensionError(
            f"unsupported source extension '{candidate.suffix}' for "
            f"'{candidate.name}'; rename the file to "
            f"'{candidate.with_suffix(SOURCE_SUFFIX).name}'"
        )
    rendered = candidate.suffix or "<none>"
    raise SourceExtensionError(
        f"unsupported source extension '{rendered}' for '{candidate.name}'; "
        f"ZLang HDL source files must end in '{SOURCE_SUFFIX}'"
    )
