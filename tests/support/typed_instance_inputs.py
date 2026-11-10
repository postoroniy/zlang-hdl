# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Generic narrow-field instance inputs; no device or application dependencies."""


def source(width=5, type_=None, kind="wire"):
    type_ = type_ or f"u{width}"
    if kind == "rv":
        child = f"""in input:rv<{type_}> out output:rv<{type_}>
        input.ready=output.ready output.valid=input.valid output.payload=input.payload"""
        binding = f"""child:Child
        child.input.payload=take?selected.value:bitcast<{type_}>(held)
        child.input.valid=enable child.output.ready=consume
        y=bitcast<bits<{width}>>(child.output.payload)
        ready=child.input.ready valid=child.output.valid"""
    else:
        child = (f"in x:{type_} out reg y:bits<{width}>=0\n"
                 f"when enable {{y<-bitcast<bits<{width}>>(x)}}"
                 if kind == "registered" else
                 f"in x:{type_} out y:bits<{width}>=bitcast<bits<{width}>>(x)")
        child = "clock clk reset rst in enable:bit\n" + child
        binding = f"""child:Child {{enable=enable x=take?selected.value:bitcast<{type_}>(held)}}
        y=child.y ready=consume valid=enable"""
    return f"""struct Packet {{ pad:bits<65> value:{type_} tail:bits<17> }}
    module Child {{ {child} }}
    module Top {{
        clock clk reset rst
        in a,b:bits<{width + 82}> in held:bits<{width}>
        in choose,take,enable,consume:bit
        out y:bits<{width}> out ready,valid:bit
        selected=choose?bitcast<Packet>(a):bitcast<Packet>(b)
        {binding}
    }}"""


def sample(width, values):
    return (((values["a"] if values["choose"] else values["b"]) >> 17)
            if values["take"] else values["held"]) & ((1 << width) - 1)
