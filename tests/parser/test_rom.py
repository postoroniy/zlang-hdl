from __future__ import annotations

import pytest

from zlang.ast import GenerateExpr, RomDecl
from zlang.parser import ParseError, parse


def test_initialized_rom_declaration_retains_initializer_and_origin() -> None:
    module = parse(
        """
        module Lookup<N=4> {
          clock clk
          reset rst
          rom coefficients : rom<u8, N> {
            read_latency 1
            init generate(i in 0..N) i
          }
        }
        """
    )

    assert len(module.roms) == 1
    rom = module.roms[0]
    assert isinstance(rom, RomDecl)
    assert rom.name == "coefficients"
    assert rom.element_type.text == "u8"
    assert rom.depth == "N"
    assert rom.read_latency == 1
    assert isinstance(rom.initializer, GenerateExpr)
    assert rom.initializer.stop == "N"
    assert rom.origin is not None
    assert rom.origin.start_line == 5
    assert rom in module.ordered_items


@pytest.mark.parametrize(
    "body",
    (
        "rom r:rom<u8,4>{read_latency 1}",
        "rom r:rom<u8,4>{init generate(i in 0..4) i}",
        "rom r:rom<u8,4>{read_latency 1 init}",
    ),
)
def test_initialized_rom_requires_latency_and_initializer(body: str) -> None:
    with pytest.raises(ParseError):
        parse(f"module Bad{{clock c reset r {body}}}")


def test_rom_keyword_is_not_lexed_as_csr_ro_access() -> None:
    module = parse(
        "module R{clock c reset r rom table:rom<u1,1>{"
        "read_latency 1 init generate(i in 0..1) 0}}"
    )
    assert module.roms[0].name == "table"
