# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Invalid callable bodies retain source diagnostics across warm parses."""

import pytest

from zlang.parser import ParseError, parse
from zlang.source import SourceSpan


@pytest.mark.parametrize("declaration", ("fn build()", "operator +(a:u8,b:u8)"))
@pytest.mark.parametrize("body,message,span", (
    ("    values = vec![]\n    values", "only immutable inferred bindings may precede a callable result", SourceSpan(2, 17, 2, 20)),
    ("    value = 1", "callable body requires one final result expression", SourceSpan(2, 5, 2, 14)),
    ("    0\n    1", "only immutable inferred bindings may precede a callable result", SourceSpan(2, 5, 2, 6)),
))
def test_invalid_callable_body_is_a_positioned_parse_error(declaration, body, message, span):
    source = declaration + " {\n" + body + "\n}\n"
    for _ in range(2):
        with pytest.raises(ParseError, match=message) as raised:
            parse(source)
        assert raised.value.diagnostic.primary.span == span
        assert raised.value.diagnostic.primary.construct == "callable body"
        assert parse("module Valid {out value:u8 value=1}").name == "Valid"
