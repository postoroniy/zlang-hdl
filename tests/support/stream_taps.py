# SPDX-License-Identifier: Apache-2.0
"""Small typed witnesses and independent packed-wire contracts for passive taps."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Payload:
    name: str
    type: str
    width: int
    declaration: str = ""
    expression: str = "raw"
    fields: tuple[tuple[str, str], ...] = (("", "expected"),)
    expected: str = "raw"


PAYLOADS = (
    Payload("bit", "bit", 1, expression="bitcast<bit>(raw)"),
    Payload("signed", "s8", 8),
    Payload(
        "struct", "Pair", 12, "struct Pair {left:s8 right:u4}",
        "Pair {left=bitcast<s8>(raw[11:4]) right=bitcast<u4>(raw[3:0])}",
        (("_left", "expected[11:4]"), ("_right", "expected[3:0]")),
    ),
    Payload(
        "enum", "Mode", 2, "enum Mode:bits<2> {Idle=0 Busy=1 Done=2}",
        "enum_decode<Mode>(raw,Mode.Idle)", expected="raw < 3 ? raw : 0",
    ),
    Payload(
        "union", "Packet", 9, "union Packet {Idle Data {value:s8}}",
        "raw[8] ? Packet.Data {value=bitcast<s8>(raw[7:0])} : Packet.Idle",
        expected="raw[8] ? raw : 0",
    ),
)


def source(payload, *, rv=False):
    ports = "in present,accept:bit" if rv else ""
    flags = """
out upstream_ready,downstream_valid,observed_valid,observed_ready,observed_transfer:bit
out reference_transfer:bit
t.input.payload=value t.input.valid=present t.output.ready=accept
result=t.output.payload tapped=t.tap_payload
upstream_ready=t.input.ready downstream_valid=t.output.valid
observed_valid=t.tap_valid observed_ready=t.tap_ready observed_transfer=t.tap_transfer
reference_transfer=present & accept
""" if rv else "t.input=value result=t.output tapped=t.tap"
    return f"""import std.stream.composition
{payload.declaration}
module Top {{
 clock clk reset rst
 in raw:{'s8' if payload.name == 'signed' else f'bits<{payload.width}>'} {ports}
 out result,tapped,reference_payload:{payload.type}
 value:{payload.type}={payload.expression}
 reference_payload=value
 t:{'RvTee' if rv else 'Tee'}<T={payload.type}>
 {flags}
}}
"""


def values(payload):
    limit = (1 << payload.width) - 1
    return tuple(sorted({0, 1, limit, limit >> 1, *(1 << i for i in range(payload.width))}))


def input_value(payload, raw):
    return raw - 256 if payload.name == "signed" and raw >= 128 else raw


def contract(payload, *, rv=False):
    inputs = ["input clk,rst", f"input [{payload.width - 1}:0] raw"]
    connections = [".clk(clk)", ".rst(rst)", ".raw(raw)"]
    declarations, assertions = [], []
    for output in ("result", "tapped", "reference_payload"):
        for suffix, expected in payload.fields:
            name = output + suffix
            width = 8 if suffix == "_left" else 4 if suffix == "_right" else payload.width
            declarations.append(f"wire [{width - 1}:0] {name};")
            connections.append(f".{name}({name})")
            assertions.append(f"assert({name}=={expected});")
    if rv:
        inputs.append("input present,accept")
        connections.extend((".present(present)", ".accept(accept)"))
        for name, expected in (
            ("upstream_ready", "accept"), ("downstream_valid", "present"),
            ("observed_valid", "present"), ("observed_ready", "accept"),
            ("observed_transfer", "present & accept"), ("reference_transfer", "present & accept"),
        ):
            declarations.append(f"wire {name};")
            connections.append(f".{name}({name})")
            assertions.append(f"assert({name}==({expected}));")
    return ("module contract(" + ",".join(inputs) + ");\n"
            + f"wire [{payload.width - 1}:0] expected={payload.expected};\n"
            + "\n".join(declarations) + "\nTop dut(" + ",".join(connections) + ");\n"
            + "always @* begin " + "".join(assertions) + " end\nendmodule\n")
