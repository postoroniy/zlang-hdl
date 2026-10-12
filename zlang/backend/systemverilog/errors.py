# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Direct-SystemVerilog diagnostic boundary."""

from zlang.diagnostics import DiagnosticError

class SystemVerilogEmissionError(DiagnosticError):
    """Typed IR is outside the supported direct-SystemVerilog subset."""

    default_code = "ZL-BACKEND-SYSTEMVERILOG-001"

    def __init__(
        self,
        message: str,
        *,
        semantic_path: tuple[str, ...] = (),
        code: str | None = None,
        primary=None,
        notes=(),
        fixes=(),
    ) -> None:
        super().__init__(
            message,
            code=code,
            primary=primary,
            notes=notes,
            fixes=fixes,
        )
        self.semantic_path = semantic_path
