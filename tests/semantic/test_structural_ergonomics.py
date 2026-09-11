from zlang.compiler import compile_source
from zlang.ir.types import EnumType
from zlang.semantic import SemanticError

import pytest


CHAIN_HEADER = """
interface PipeIfc { clock clk reset rst in rx:rv<u8> out tx:rv<u8> }
module Stage : PipeIfc {
  tx.payload=rx.payload
  tx.valid=rx.valid
  rx.ready=tx.ready
}
"""


def test_qualified_fsm_initializer_lowers_to_existing_rule_state_ir() -> None:
    result = compile_source(
        "enum Phase { Idle Run } module M { clock clk reset rst "
        "fsm phase = Phase.Idle { Idle { -> Run {} } Run { hold } } "
        "out y:bit=0 }",
    )
    assert len(result.ir.registers) == 1
    assert isinstance(result.ir.registers[0].type, EnumType)
    assert result.ir.registers[0].type.name == "Phase"
    assert len(result.ir.rules) == 1


def test_named_transform_connection_chain_is_exact_pairwise_connection_sugar() -> None:
    concise = compile_source(
        CHAIN_HEADER
        + "module Top { clock clk reset rst in input:rv<u8> out output:rv<u8> "
        "a:Stage b:Stage input -> a -> b -> output }",
    ).ir
    explicit = compile_source(
        CHAIN_HEADER
        + "module Top { clock clk reset rst in input:rv<u8> out output:rv<u8> "
        "a:Stage b:Stage input -> a.rx a.tx -> b.rx b.tx -> output }",
    ).ir
    assert concise.hierarchical_connections == explicit.hierarchical_connections
    assert tuple(
        (edge.source.owner, edge.source.name, edge.destination.owner, edge.destination.name)
        for edge in concise.hierarchical_connections
    ) == (
        ("Top", "input", "a", "rx"),
        ("a", "tx", "b", "rx"),
        ("b", "tx", "Top", "output"),
    )


@pytest.mark.parametrize(
    ("stage", "message"),
    (
        (
            "module Stage { in rx:rv<u8> out tx:rv<u8> "
            "tx.payload=rx.payload tx.valid=rx.valid rx.ready=tx.ready }",
            "must conform to one named module interface",
        ),
        (
            "interface Wide { in a:rv<u8> in b:rv<u8> out y:rv<u8> } "
            "module Stage:Wide { y.payload=a.payload y.valid=a.valid "
            "a.ready=y.ready b.ready=0 }",
            "requires exactly one protocol input and one protocol output",
        ),
    ),
)
def test_connection_chain_rejects_noncanonical_intermediate_interfaces(
    stage: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        compile_source(
            stage
            + " module Top { in input:rv<u8> out output:rv<u8> "
            "s:Stage input -> s -> output }",
        )
