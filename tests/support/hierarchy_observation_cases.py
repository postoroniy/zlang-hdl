# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Small independent fixtures for generated locals and endpoint observations."""

GENERATED_LOCALS = """
module EchoLane {
    in sample:u8
    out result:u8 = sample
}
module GeneratedLocals {
    in samples:vec<2,u8>
    out results:vec<2,u8>
    lane[2]:EchoLane
    generate(k in 0..2) {
        chosen=samples[k]
        lane[k].sample=chosen
    }
    results=generate(k in 0..2) lane[k].result
}
"""

CHANNELS = """
struct ObservationWord { value:u8 complement:u8 }
protocol ObservationBus {
    role requester
    role responder
    channel request:rv<u8> requester -> responder
    channel response:rv<ObservationWord> responder -> requester
}
module ObservationRequester {
    clock clk reset rst
    in enable,consume:bit
    in data:u8
    interface bus:ObservationBus.requester @clk
    out reg accepted:u8=0
    bus.request.valid=enable
    bus.request.payload=data
    bus.response.ready=consume
    when bus.response.transfer { accepted <- truncate<8>(accepted+1) }
}
module ObservationResponder {
    clock clk reset rst
    interface bus:ObservationBus.responder @clk
    bus.request.ready=bus.response.ready
    bus.response.valid=bus.request.valid
    bus.response.payload=ObservationWord {
        value=bus.request.payload complement=~bus.request.payload
    }
}
"""


def observation_source(*, manual: bool = False, alias: bool = True) -> str:
    if manual:
        wiring = """
        peer:ObservationResponder
        peer.bus.request.valid=enable
        peer.bus.request.payload=data
        peer.bus.response.ready=consume
        """
        incoming, outgoing = "peer.bus.request", "peer.bus.response"
        count = ""
        count_output = ""
    else:
        wiring = """
        requester:ObservationRequester { enable=enable consume=consume data=data }
        peer:ObservationResponder
        requester.bus -> peer.bus
        """
        incoming, outgoing = "requester.bus.response", "peer.bus.request"
        count = "accepted=requester.accepted"
        count_output = "out accepted:u8"
    values = (
        ("observed", f"{incoming}.payload" + ("" if manual else ".value")),
        ("observed_valid", f"{incoming}.valid"),
        ("observed_ready", f"{outgoing}.ready"),
        ("fired", f"{outgoing if manual else incoming}.transfer"),
    )
    assignments = "\n".join(
        f"local_{name}={expression}\n{name}=local_{name}"
        if alias else f"{name}={expression}"
        for name, expression in values
    )
    return CHANNELS + f"""
    module EndpointObservations {{
        clock clk reset rst
        in enable,consume:bit
        in data:u8
        out observed:u8
        out observed_valid,observed_ready,fired:bit
        {count_output}
        {wiring}
        {assignments}
        {count}
    }}
    """


def delayed_observation_source() -> str:
    return (observation_source(manual=True)
            .replace("peer.bus.response.ready=consume", "peer.bus.response.ready=delay<1>(consume)")
            .replace("out observed:u8", "out other:bit=delay<1>(enable)\n out observed:u8"))
