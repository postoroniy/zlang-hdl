from zlang.parser import parse


SOURCE = """
interface AddIfc {
    in a : u8
    in b : u8
    out y : u9
    timing { latency 0 ii 1 }
}

fn add_model(a : u8, b : u8) -> u9 { a + b }

extern module VendorAdd : AddIfc { model add_model }

module Top {
    in a : u8
    in b : u8
    out y : u9
    inst dut : VendorAdd { a b }
    y = dut.y
}
"""


def test_parser_retains_external_model_and_named_interface() -> None:
    syntax = parse(SOURCE)
    external = syntax.submodules[0]
    assert external.name == "VendorAdd"
    assert external.conforms_to is not None
    assert external.conforms_to.name == "AddIfc"
    assert external.external_model == "add_model"
    assert external.external_origin is not None
