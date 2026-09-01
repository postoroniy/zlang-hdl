from __future__ import annotations

from zlang.parser import parse


def test_indexed_hierarchical_endpoints_preserve_source_selector() -> None:
    module = parse(
        "module Child { clock clk reset rst in rx:rv<u8> out tx:rv<u8> "
        "rx.ready=tx.ready tx.payload=rx.payload tx.valid=rx.valid } "
        "module Top<K=1> { clock clk reset rst in rx:rv<u8> out tx:rv<u8> "
        "inst child[2]:Child connect rx -> child[0].rx "
        "connect child[0].tx -> child[K].rx connect child[1].tx -> tx }"
    )

    assert [(item.source, item.destination) for item in module.connections] == [
        ("rx", "child[0].rx"),
        ("child[0].tx", "child[K].rx"),
        ("child[1].tx", "tx"),
    ]


def test_generate_binder_is_accepted_in_hierarchical_endpoint_index() -> None:
    module = parse(
        "module Source { clock clk reset rst out tx:rv<u8> "
        "tx.payload=1 tx.valid=1 } "
        "module Sink { clock clk reset rst in rx:rv<u8> rx.ready=1 } "
        "module Top { clock clk reset rst inst source[2]:Source "
        "inst sink[2]:Sink generate(i in 0..2) { "
        "connect source[i].tx -> sink[i].rx } }"
    )

    assert module.generate_blocks[0].items[0].source == "source[i].tx"
    assert module.generate_blocks[0].items[0].destination == "sink[i].rx"


def test_indexed_hierarchical_endpoint_normalizes_numeric_literal_spelling() -> None:
    module = parse(
        "module Source { clock clk reset rst out tx:rv<u8> "
        "tx.payload=1 tx.valid=1 } "
        "module Sink { clock clk reset rst in rx:rv<u8> rx.ready=1 } "
        "module Top { clock clk reset rst inst source[2]:Source "
        "inst sink[2]:Sink connect source[0x0].tx -> sink[0b1].rx }"
    )

    assert module.connections[0].source == "source[0].tx"
    assert module.connections[0].destination == "sink[1].rx"
