from __future__ import annotations

import json

import pytest

from zlang.backend.clash import ClashEmissionError
from zlang.backend.systemverilog import SystemVerilogEmissionError
from zlang.diagnostics import DIAGNOSTIC_SCHEMA, Diagnostic, DiagnosticError
from zlang.parser import ParseError, parse
from zlang.semantic import SemanticError, analyze
from zlang.source import SourceOrigin, SourceSpan


def test_diagnostic_json_schema_is_stable_and_deterministic() -> None:
    origin = SourceOrigin(SourceSpan(2, 3, 2, 8), "operator +")
    diagnostic = Diagnostic(
        "ZL-WIDTH-ASSIGNMENT",
        "cannot assign u9 expression to u8 output 'y'",
        origin,
        ("the expression produces one carry bit",),
        ("use an explicit exact-width conversion",),
    )

    encoded = diagnostic.to_json()
    assert encoded == diagnostic.to_json()
    assert Diagnostic.from_json(encoded) == diagnostic
    assert json.loads(encoded) == {
        "schema": DIAGNOSTIC_SCHEMA,
        "severity": "error",
        "code": "ZL-WIDTH-ASSIGNMENT",
        "message": "cannot assign u9 expression to u8 output 'y'",
        "primary": {
            "source_unit": None,
            "digest": None,
            "span": {
                "start_line": 2,
                "start_column": 3,
                "end_line": 2,
                "end_column": 8,
            },
            "construct": "operator +",
        },
        "notes": ["the expression produces one carry bit"],
        "fixes": ["use an explicit exact-width conversion"],
    }


@pytest.mark.parametrize(
    ("error", "code"),
    (
        (ParseError("legacy parse"), "ZL-PARSE-001"),
        (SemanticError("legacy semantic"), "ZL-SEMANTIC-001"),
        (ClashEmissionError("legacy clash"), "ZL-BACKEND-CLASH-001"),
        (
            SystemVerilogEmissionError("legacy systemverilog"),
            "ZL-BACKEND-SYSTEMVERILOG-001",
        ),
    ),
)
def test_public_errors_preserve_legacy_string(error: DiagnosticError, code: str) -> None:
    assert str(error) == error.args[0]
    assert error.code == code
    assert error.diagnostic.message == str(error)


def test_width_error_has_structured_code_origin_and_fix() -> None:
    with pytest.raises(SemanticError) as raised:
        analyze(parse("module Bad { in a:u8 out y:u8 y=a+a }"))

    error = raised.value
    assert str(error) == "cannot assign u9 expression to u8 output 'y'"
    assert error.code == "ZL-WIDTH-ASSIGNMENT"
    assert error.primary is not None
    assert error.primary.construct == "operator +"
    assert error.fixes == ("use an explicit exact-width conversion",)


def test_import_and_protocol_categories_are_structured() -> None:
    with pytest.raises(SemanticError) as duplicate:
        analyze(
            parse(
                "import std.bus.reg\n"
                "import std.bus.reg\n"
                "module Bad { out y:u8 y=0 }"
            )
        )
    assert duplicate.value.code == "ZL-IMPORT-DUPLICATE"

    with pytest.raises(SemanticError) as protocol:
        analyze(
            parse(
                "module Bad {\n"
                "  in tx:rv<u8>\n"
                "  out rx:rv<u16>\n"
                "  connect tx -> rx\n"
                "}"
            )
        )
    assert protocol.value.code == "ZL-PROTOCOL-TYPE"


def test_systemverilog_error_retains_semantic_path_with_diagnostic() -> None:
    error = SystemVerilogEmissionError(
        "missing backend binding",
        semantic_path=("Top", "child", "value"),
        code="ZL-BACKEND-BINDING",
        fixes=("publish the typed binding",),
    )
    assert error.semantic_path == ("Top", "child", "value")
    assert error.code == "ZL-BACKEND-BINDING"
    assert str(error) == "missing backend binding"
