# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Parser-owned public diagnostics independent of grammar rule modules."""

from zlang.diagnostics import DiagnosticError


class ParseError(DiagnosticError):
    """A source file does not conform to the ZLang grammar."""

    default_code = "ZL-PARSE-001"


__all__ = ["ParseError"]
