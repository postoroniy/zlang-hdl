from zlang.compiler import compile_source
from zlang.ir.formal_predicates import Mux


def contract(source: str, name: str = "g"):
    design = compile_source(source).formal_design
    return next(
        item for item in design.properties
        if item.generated_from == f"contract:{name}"
    )


def test_switch_contract_lowers_to_typed_structured_muxes() -> None:
    item = contract(
        "module SwitchContract { clock c reset r in select:u2 in a:bit "
        "in b:bit out y:bit y=a guarantee g @ c disable iff r { "
        "switch select { 0=>a 1=>b else=>y } } }"
    )

    assert isinstance(item.predicate, Mux)
    assert item.predicate.observation_ids() == (
        "port:select", "port:a", "port:b", "port:y",
    )


def test_ready_valid_and_packet_transfer_use_valid_and_ready_observations() -> None:
    rv = contract(
        "module RVContract { clock c reset r in rx:rv<u8> out tx:rv<u8> "
        "connect rx -> tx { buffer 1 } "
        "guarantee g @ c disable iff r { rx.transfer } }"
    )
    assert rv.predicate.observation_ids() == (
        "port:rx.valid", "port:rx.ready",
    )

    packet = contract(
        "module PacketContract { clock c reset r in high:packet<u8> "
        "in low:packet<u8> out tx:packet<u8> "
        "arbiter [high,low] -> tx { policy fixed_priority grant packet } "
        "guarantee g @ c disable iff r { high.transfer } }"
    )
    assert packet.predicate.observation_ids() == (
        "port:high.valid", "port:high.ready",
    )


def test_credit_transfers_use_send_without_a_fictitious_transfer_signal() -> None:
    credit = contract(
        "module CreditContract { clock c reset r in data:u8 in issue:bit "
        "out tx:credit<u8,2> tx.payload=data tx.send=issue "
        "guarantee g @ c disable iff r { tx.transfer } }"
    )
    assert credit.predicate.observation_ids() == ("port:tx.send",)

    vc = contract(
        "module VCContract { clock c reset r in data:u8 in channel:u1 "
        "in issue:bit out tx:vc_credit<u8,2,2> tx.payload=data "
        "tx.vc=channel tx.send=issue "
        "guarantee g @ c disable iff r { tx.transfer } }"
    )
    assert vc.predicate.observation_ids() == ("port:tx.send",)


def test_request_response_transfer_uses_channel_valid_and_ready() -> None:
    item = contract(
        "struct Req { data:u8 } struct Rsp { data:u8 } "
        "module RRContract { clock c reset r "
        "interface mem:request_response<Req,Rsp> { "
        "max_outstanding 1 ordering in_order } "
        "in req:Req in issue:bit in consume:bit out rsp:Rsp "
        "mem.request.payload=req mem.request.valid=issue "
        "mem.response.ready=consume rsp=mem.response.payload "
        "guarantee g @ c disable iff r { mem.request.transfer } }"
    )
    assert item.predicate.observation_ids() == (
        "port:mem.request.valid", "port:mem.request.ready",
    )
