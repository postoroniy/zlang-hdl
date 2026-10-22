# Stream composition, CRC and fixed-point FIR

These are ordinary ZLang modules. Use `import std.stream.composition`,
`import std.coding.crc` or `import std.dsp.fir`; no `implement` block or
target-specific architecture is required. Stateful components use one
`clock clk reset rst` domain. Connect only compatible clock/reset contracts;
these components do not perform CDC.

## Broadcast and join

`RvFork2<T>` has `input:rv<T>` and `output0/output1:rv<T>`. It stores one
payload and remembers delivery to each consumer independently. A consumer
that accepted early does not receive the item again while the other stalls.
The last delivery can retire together with acceptance of a replacement item.
There is no empty-slot combinational bypass.

`RvJoin2<A,B>` has `input0:rv<A>`, `input1:rv<B>` and
`output:rv<StreamPair<A,B>>`, whose fields are `left:A` and `right:B`.
Each input has its own slot. The k-th accepted element of input0 pairs with
the k-th accepted element of input1, regardless of their arrival cycles.
Both slots may refill on output retirement. Stalls hold the pair unchanged.
Reset discards pending items. Payload types remain exact, including aggregates;
there is no implicit `default<T>`, adapter or protocol conversion.

## CRC-32 byte frames

`crc_step_reflected<W>(state:bits<W>, data_bit:bit, polynomial:bits<W>)`
returns an exact W-bit raw state. Specify W explicitly, for example
`crc_step_reflected<W=32>(state, data_bit, polynomial)`. W must be positive.
The function does not initialize, finalize or reverse a word.

`RvCrc32Byte` accepts `input:rv<CrcByteBeat>`; `CrcByteBeat` contains
`data:bits<8>` and `last:bit`. Its `output:rv<bits<32>>` produces one checksum
for each accepted last byte. Input bytes are processed LSB-first with reflected
polynomial `0xEDB88320`, raw initialization `0xFFFFFFFF` and final XOR
`0xFFFFFFFF`. Thus `123456789` gives `0xCBF43926`. This is the CRC convention
in [RFC 1952's sample code](https://www.rfc-editor.org/rfc/rfc1952.html).

Within a frame the engine can accept one byte per cycle. A pending result
blocks the next frame, except when that result retires on the same edge as
the next frame's first byte. Reset drops the partial frame and pending result.
There is no empty-frame transaction, byte-enable field or automatic framing.

The [framed broadcast example](../examples/framed_crc.zhl) sends each byte
both to the CRC engine and to an independently backpressured mirror:

```sh
.venv/bin/zlang sim examples/framed_crc.zhl --top FramedCrc \
  --events examples/framed_crc.events.jsonl --trace build/framed_crc.fst --json
.venv/bin/zlang examples/framed_crc.zhl --top FramedCrc \
  --systemverilog build/FramedCrc.sv --verilator-lint
```

Its checked-in events send `abc`; the checksum is `0x352441C2`.

## Parallel FIR

`RvFirFixedExact<SW,SF,CW,CF,N,coefficients>` accepts
`input:rv<fixed<SW,SF>>` and returns
`output:rv<fixed<SW+CW+ceil_log2(N),SF+CF>>`.
Use named specialization arguments. The checked derived parameter `H=N-1`
is only the history dimension; leave it at its default.

Coefficients are an immutable compile-time `vec<N,fixed<CW,CF>>`, not runtime
ports. Declare a typed local vector and pass its name as `coefficients=taps`.
`coefficients[0]` multiplies the current accepted sample, and subsequent
coefficients multiply earlier accepted samples. Products are full-width;
the existing `dot` supplies its deterministic balanced exact reduction.

N must be at least one. N=1 has no history storage. Reset zeroes history and
clears output validity. The output is one buffered stage: one-cycle unstalled
latency and II=1. A full stalled output blocks acceptance and leaves history
unchanged. Retirement and next acceptance can occur on the same edge.

`RvFirFixedNearestEvenSaturate<SW,SF,CW,CF,N,OW,OF,coefficients>` uses that
exact core, then rounds once to `fixed<OW,OF>` with nearest-even rounding and
saturation. It adds no pipeline cycle. Other numerical policies require an
explicit caller-owned conversion after the exact core. There is no coefficient
reload, decimation, interpolation, folded scheduler or implicit DSP mapping.
For those explicit library variants, see [reloadable/folded FIR](stdlib-reloadable-fir.md)
and [multirate filters](stdlib-multirate-coding.md); the immutable modules here
retain their existing interfaces and one-stage behavior.

Run the [eight-tap example](../examples/stream_fir8.zhl):

```sh
.venv/bin/zlang sim examples/stream_fir8.zhl --top StreamFir8 \
  --events examples/stream_fir8.events.jsonl --trace build/stream_fir8.fst --json
.venv/bin/zlang examples/stream_fir8.zhl --top StreamFir8 \
  --systemverilog build/StreamFir8.sv --verilator-lint
```

Simulation integer values are raw fixed-point bits: with SF=12, `4096`
represents 1.0. The example sends that impulse followed by zero samples.
Accepted output raw values are `128,384,640,896,896,640,384,128,0`.
When adapting events to introduce stalls, hold each valid input payload until
its transfer; one JSONL record is a simulation event, not necessarily a transfer.

Numerical/stream tests cover N=1/2/3/8/16/32. Formal evidence is attached to
specific specializations and properties, not a blanket proof of every generic
configuration. Synthesis cell counts are not routed timing evidence.
