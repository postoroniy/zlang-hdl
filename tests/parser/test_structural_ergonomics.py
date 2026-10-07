from zlang.ast import ConnectionChainDecl
from zlang.parser import parse


def test_compact_clock_reset_pair_is_two_existing_declarations() -> None:
    syntax = parse("module M { clock clk reset rst reg x:u8=0 out y:u8=x }")
    assert syntax.clocks == ("clk",)
    assert syntax.resets == ("rst",)


def test_qualified_fsm_initializer_uniquely_supplies_nominal_type() -> None:
    syntax = parse(
        "enum Phase { Idle Run } module M { clock clk reset rst "
        "fsm phase = Phase.Idle { Idle { -> Run {} } Run { hold } } "
        "out y:bit=0 }"
    )
    declaration = syntax.fsms[0]
    assert declaration.type_name.text == "Phase"
    assert declaration.initial_member == "Idle"


def test_option_free_connection_chain_retains_instance_sequence() -> None:
    syntax = parse(
        "module Stage { in rx:rv<u8> out tx:rv<u8> "
        "tx.payload=rx.payload tx.valid=rx.valid rx.ready=tx.ready } "
        "module Top { in input:rv<u8> out output:rv<u8> "
        "a:Stage b:Stage input -> a -> b -> output }"
    )
    declaration = next(
        item for item in syntax.ordered_items
        if isinstance(item, ConnectionChainDecl)
    )
    assert declaration.endpoints == ("input", "a", "b", "output")
    assert declaration.origin is not None
