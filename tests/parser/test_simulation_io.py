# SPDX-License-Identifier: Apache-2.0
import pytest

from zlang.parser import parse
from zlang.parser.errors import ParseError
from zlang.simulation_io_model import IORead, IOWhen, IOWrite


def test_contextual_commands_keep_expression_origins():
    module = parse("""module T {clock clk in a:u8 out y:u8 y=a
simulation vectors @clk {
 when a==0 {read source {sample -> a} else {finish}}
 write result {value=y}
}}""")
    (block,) = module.simulation_blocks
    assert block.name == "vectors" and block.clock == "clk"
    assert isinstance(block.commands[0], IOWhen)
    assert isinstance(block.commands[0].body[0], IORead)
    assert isinstance(block.commands[1], IOWrite)
    assert block.commands[1].bindings[0][1].origin.start_line == 4


@pytest.mark.parametrize("name", ["read", "write", "finish", "simulation"])
def test_new_commands_do_not_reserve_ordinary_identifiers(name):
    module = parse(f"module T {{in {name}:u8 out y:u8 y={name}}}")
    assert module.ports[0].name == name


@pytest.mark.parametrize(
    "body",
    [
        "read source {a -> a}",
        'read "file.jsonl" {a -> a} else {finish}',
        "write result {}",
        "while 1 {finish}",
        "read source {} else {finish}",
    ],
)
def test_incomplete_or_procedural_simulation_syntax_rejected(body):
    with pytest.raises(ParseError):
        parse(f"module T {{clock clk in a:u8 simulation io @clk {{{body}}}}}")


@pytest.mark.parametrize("separator", ["", " // comment\n", " /* comment */ "])
def test_finish_binding_after_an_expression_remains_contextual(separator):
    parse(f"""module T {{in a:bit out y:bit y=a
finish{separator}=a
out z:bit z=finish
}}""")
    parse(f"""module T {{clock clk reset rst in a:bit out y:bit y=a
simulation io @clk {{drive a=0 finish{separator}}}}}""")


def test_real_spi_local_finish_keeps_ordinary_binding_semantics():
    from pathlib import Path

    parse(
        (Path(__file__).resolve().parents[2] / "stdlib/peripheral/spi.zhl").read_text()
    )


@pytest.mark.parametrize("name", ["finish", "read", "write", "simulation"])
def test_contextual_names_remain_callable_bindings_results_and_calls(name):
    parse(f"""fn {name}(x:u8)->u8 {{x}}
fn probe(x:u8)->u8 {{{name}=x {name}}}
module T {{in a:u8 out y:u8 y={name}(a)}}""")


@pytest.mark.parametrize("name", ["stop", "foobar", "return"])
def test_unknown_bare_simulation_commands_are_rejected(name):
    with pytest.raises(ParseError, match="unknown simulation command"):
        parse(f"module T {{clock clk reset rst simulation io @clk {{{name}}}}}")
