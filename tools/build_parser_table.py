# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Verify or regenerate the trusted, packaged Lark parse table.

The table is an ordinary build artifact derived from grammar.lark. Runtime
loading checks both its SHA-256 and the exact Lark version before unpickling.
Never load a parser table from a user-writable cache directory.
"""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
from pathlib import Path

import lark
from lark import Lark


ROOT = Path(__file__).resolve().parents[1]
GRAMMAR = ROOT / "zlang/parser/grammar.lark"
TABLE = ROOT / "zlang/parser/lalr-1.3.1.larkbin"


def generate() -> bytes:
    grammar = GRAMMAR.read_text(encoding="utf-8")
    parser = Lark(grammar, parser="lalr", propagate_positions=True)
    stream = BytesIO()
    parser.save(stream)
    return stream.getvalue()


def main() -> int:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("--write", action="store_true")
    args = argument_parser.parse_args()
    if lark.__version__ != "1.3.1":
        argument_parser.error("parser table generation requires Lark 1.3.1")
    if args.write:
        generated = generate()
        TABLE.write_bytes(generated)
        print("Update _PARSER_TABLE_SHA256 in zlang/parser/parser.py:")
    elif not TABLE.is_file():
        argument_parser.error("packaged parser table is missing; rerun with --write")
    else:
        generated = TABLE.read_bytes()
    from zlang.parser import parser as parser_module

    if not args.write and (
        parser_module._PARSER_TABLE_GRAMMAR_SHA256
        != hashlib.sha256(GRAMMAR.read_bytes()).hexdigest()
        or parser_module._PARSER_TABLE_SHA256 != hashlib.sha256(generated).hexdigest()
    ):
        argument_parser.error("packaged parser identity does not match the source constants")
    print(
        "grammar sha256:", hashlib.sha256(GRAMMAR.read_bytes()).hexdigest(),
        "table sha256:", hashlib.sha256(generated).hexdigest(),
        "table bytes:", len(generated),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
