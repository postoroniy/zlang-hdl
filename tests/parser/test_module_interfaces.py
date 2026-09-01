from __future__ import annotations

from zlang.ast.nodes import ModuleInterfaceDecl, TypeName
from zlang.parser import parse


def test_named_module_interface_parser_retains_parameters_ports_and_reference() -> None:
    module = parse(
        """
interface FirIfc<type Sample, N=4> {
    clock clk
    reset rst
    in x : Sample @clk
    out y : vec<N,Sample> @clk
    timing { latency 4 ii 1 }
}

module Fir<type Sample, N=4> : FirIfc<Sample,N> {
    clock clk
    reset rst
    in x : Sample @clk
    out y : vec<N,Sample> @clk
    y = generate(i in 0..N) x
    timing { latency 4 ii 1 }
}
"""
    )

    assert module.conforms_to is not None
    assert module.conforms_to.name == "FirIfc"
    assert tuple(argument.value for argument in module.conforms_to.arguments) == (
        "Sample",
        "N",
    )
    assert module.declared_parameters == module.parameters
    assert len(module.module_interfaces) == 1
    declaration = module.module_interfaces[0]
    assert isinstance(declaration, ModuleInterfaceDecl)
    assert tuple((item.name, item.kind, item.default) for item in declaration.parameters) == (
        ("Sample", "type", None),
        ("N", "value", 4),
    )
    assert declaration.ports[0].type_name == TypeName("Sample")
    assert declaration.timing is not None
    assert declaration.timing.latency == 4


def test_grouped_interface_ports_remain_one_syntax_declaration() -> None:
    module = parse(
        """
interface PairIfc { in a, b : u8 out y : u9 }
module Pair : PairIfc { in a, b : u8 out y : u9 y = a + b }
"""
    )
    declaration = module.module_interfaces[0]
    assert declaration.ports[0].names == ("a", "b")
