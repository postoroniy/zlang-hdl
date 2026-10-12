# Standards-based hardware conversion

Use this reference for IEEE, AXI/AHB/APB/Wishbone, Ethernet, USB, cryptographic,
coding, FFT, or other externally specified designs.

Maintain a compact decision table before implementation:

```text
fact | authoritative source | verified / local architecture choice / unknown
```

An older BSV/Verilog/MATLAB implementation is evidence, not authority when it
conflicts with the governing standard. Do not copy code or test vectors until
their license permits it; prefer independently expressed algorithms and
independent oracles.

Freeze the externally observable contract for one module:

- exact ports/protocol direction and accepted-transfer event;
- widths, signedness, packed/sequence order, and field encoding;
- clock/reset domain and reset epoch;
- state/storage latency and collision behavior;
- backpressure, framing, first/last/tail/padding behavior;
- numeric intermediate widths, constants, scale, rounding, and saturation;
- latency and II;
- independent reference vectors and deliberate mutations.

For LFSR/CRC/scrambler/coding/interleaving, define bit orientation and the exact
state advance per accepted beat. A multi-bit beat advances a bit-serial
recurrence by the complete logical beat, not once.

For FFT/IFFT and fixed DSP, define twiddle encoding, stage topology, order,
intermediate widths, and the single intended quantization boundary before
optimization. Never move narrowing or rounding to improve QoR without a new
bit-exact contract.

Standard buses should use the source-authored `std.bus.*` modules and their
existing oracle tests. Do not add AXI/AHB/APB/Wishbone special cases to semantic
or backend code.

If the current language cannot express the design without repetitive source,
first distinguish:

1. a real semantic capability gap;
2. a parser/type/backend bug in an already supported feature;
3. missing stdlib composition;
4. a local architecture choice that can use current source forms.

Minimize a general reproducer before proposing language lowering. Do not invent
syntax or weaken exact semantics inside an application project.
