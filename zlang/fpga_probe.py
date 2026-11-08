"""Explicit board-level stimulus wrapper for physical qualification, not core QoR."""

from __future__ import annotations

from zlang.backend.systemverilog import emit_target_artifact
from zlang.compiler import compile_source
from zlang.ir.equivalence import BindingSide


INTERFACE = """module ZlangPhysicalProbe {
    clock clk
    reset rst_n @clk { mode synchronous polarity active_low power_up unspecified }
    out led:bit
    led=0
}"""


def physical_probe(module, artifact):
    """Return a complete, ABI-bound fixture plus its ordinary board-port product.

    Every functional input receives registered changing data; all output bits
    reach the observation checksum. Functional clocks are explicitly tied to
    the board oscillator in this physical fixture only. This does not qualify
    independent-clock behavior, hardware capture or isolated-core timing/area.
    """
    interface = compile_source(INTERFACE)
    interface_artifact = emit_target_artifact(
        interface.ir, interface.implementation_graph
    )
    clocks = {d.clock for d in module.clock_domains}
    resets = {d.reset for d in module.clock_domains}
    if (
        not clocks
        or None in resets
        or any(
            d.edge.value != "rising"
            or d.reset_mode.value != "synchronous"
            or d.reset_polarity.value != "active_high"
            for d in module.clock_domains
        )
    ):
        raise ValueError(
            "physical probe requires explicit rising/synchronous active-high core domains"
        )
    bindings = tuple(
        b
        for b in artifact.bindings
        if b.side is BindingSide.IMPLEMENTATION and b.physical_available
    )
    expected = {
        leaf.external_name: leaf.width for leaf in module.top_physical_abi.leaves
    }
    actual = {b.rtl_path: b.width for b in bindings}
    if actual != expected or len(actual) != len(bindings):
        raise ValueError(
            "physical probe bindings do not exactly cover the compiler ABI"
        )
    ports, outputs, lines = {}, [], []
    for index, b in enumerate(bindings):
        if not b.physical_available or b.rtl_module != artifact.module:
            raise ValueError("physical probe requires complete top ABI")
        if b.rtl_path in ports:
            raise ValueError(
                "physical probe does not accept overlapping physical bindings"
            )
        logical = ".".join(b.member_path)
        if logical in clocks:
            signal = "clk"
        elif logical in resets:
            signal = "~rst_n"
        elif b.role.value == "input":
            signal = f"probe_input{index}"
            lines.extend(
                (
                    f"  reg [{b.width - 1}:0] {signal};",
                    f"  always @(posedge clk) if(!rst_n) {signal}<={b.width}'d{(index + 1) & ((1 << b.width) - 1)};",
                    f"    else {signal}<={signal}+{b.width}'d{(2 * index + 1) & ((1 << b.width) - 1)};",
                )
            )
        elif b.role.value == "output":
            signal = f"probe_output{index}"
            lines.append(f"  wire [{b.width - 1}:0] {signal};")
            outputs.append(f"^{signal}")
        else:
            raise ValueError("unsupported physical probe ABI role")
        ports[b.rtl_path] = signal
    if not outputs:
        raise ValueError("physical probe requires observed output bits")
    lines.append(
        f"  {artifact.module} dut("
        + ",".join(f".{p}({s})" for p, s in ports.items())
        + ");"
    )
    lines.append(
        "  reg checksum; always @(posedge clk) if(!rst_n) checksum<=0; else checksum<=checksum ^ ("
        + " ^ ".join(outputs)
        + ");"
    )
    lines.append("  assign led=checksum;")
    text = (
        "module ZlangPhysicalProbe(input clk,rst_n,output led);\n"
        + "\n".join(lines)
        + "\nendmodule\n"
    )
    return interface.ir, interface_artifact, text
