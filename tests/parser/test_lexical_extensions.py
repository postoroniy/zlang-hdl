from __future__ import annotations

import pytest

from zlang.parser import ParseError, parse


def test_block_comments_are_whitespace_between_tokens() -> None:
    module = parse("module /* split */ M { out y : /* type */ u8 y = 0b1010_1100 }")
    assert module.name == "M"


@pytest.mark.parametrize("literal", ("0b0", "0B1111", "0b1010_1100"))
def test_binary_literals_parse(literal: str) -> None:
    assert parse(f"module M {{ out y:u16 y={literal} }}").name == "M"


@pytest.mark.parametrize("literal", ("0b", "0b2", "0b_1", "0b1_", "0b1__0"))
def test_invalid_binary_literals_are_rejected(literal: str) -> None:
    with pytest.raises(ParseError, match="syntax error"):
        parse(f"module M {{ out y:u16 y={literal} }}")


def test_unterminated_block_comment_is_rejected() -> None:
    with pytest.raises(ParseError, match="line 1, column 1"):
        parse("/* no end\nmodule M { out y:u8 y=0 }")


def test_resize_operator_prefix_does_not_capture_an_identifier() -> None:
    module = parse(
        "module M { in x:u8 out y:u8 extended = x y = extended }"
    )
    assert module.name == "M"


def test_csr_access_prefix_does_not_capture_an_identifier() -> None:
    module = parse(
        "module M { in word:u8 out y:u8 word_bits:u8=word y=word_bits }"
    )
    assert module.name == "M"


@pytest.mark.parametrize("name", ("ifft_bin", "map_word", "zero_symbol"))
def test_keyword_prefixes_remain_valid_identifiers(name: str) -> None:
    module = parse(
        f"module M {{ in x:u8 out y:u8 {name}:u8=x y={name} }}"
    )
    assert module.name == "M"


def test_keyword_boundaries_preserve_if_and_map_syntax() -> None:
    parse("module M { in x:u8 out y:u8 y = if x == 0 { 1 } else { 2 } }")
    parse(
        "module M { in x:vec<2,u8> out y:vec<2,u8> "
        "y = map(i in 0..2) { x[i] } }"
    )
