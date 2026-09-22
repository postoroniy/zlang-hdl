"""The trusted LALR table is versioned and has a safe grammar fallback."""

from __future__ import annotations

import hashlib
from importlib.resources import files

from zlang.parser import parser as parser_module


def test_packaged_lalr_table_matches_grammar_and_parses() -> None:
    payload = files("zlang.parser").joinpath("lalr-1.3.1.larkbin").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == parser_module._PARSER_TABLE_SHA256
    parser = parser_module._load_packaged_parser()
    assert parser is not None
    assert parser.options.propagate_positions
    assert parser.parse("module Top {}") is not None


def test_mismatched_parser_version_or_grammar_uses_fallback(monkeypatch) -> None:
    monkeypatch.setattr(parser_module.lark, "__version__", "0.0-invalid")
    assert parser_module._load_packaged_parser() is None
    monkeypatch.setattr(parser_module, "_GRAMMAR", "module: INVALID")
    assert parser_module._load_packaged_parser() is None
