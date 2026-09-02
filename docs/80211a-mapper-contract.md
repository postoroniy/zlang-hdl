# 802.11a Mapper contract

Status: historical bounded-slice evidence. The executable project now uses the
canonical `src/mapper.zhl` hierarchy and `IeeeMapper64` naming.

This document freezes the bounded mapper slice for the standalone
`80211a_transmitter` validation project. IEEE 802.11a is the behavioral source
of truth. The historical Bluespec implementation is an implementation clue and
compatibility reference only; when it disagrees with IEEE, the IEEE behavior is
used and the discrepancy is recorded rather than copied.

## Interface and framing

`Mapper48` accepts `rv<RateWord48>` chunks from the interleaver and emits one
`rv<ComplexRawMessage<64>>` per complete OFDM symbol. The 64-vector uses the
canonical FFT-bin order `[0..63]`, with DC at index 32:

```text
indices  0..5    null
         6..10   data (5)
         11      pilot
         12..24  data (13)
         25      pilot
         26..31  data (6)
         32      DC/null
         33..38  data (6)
         39      pilot
         40..52  data (13)
         53      pilot
         54..58  data (5)
         59..63  null
```

The input rate is `1`, `2`, or `4`. A rate-one chunk completes a symbol; rates
two and four require two and four accepted chunks, respectively. A zero tag
continues the retained rate. The interleaver's frozen mapper-facing ABI repeats
the same effective nonzero tag on every serialized chunk, so that exact repeat
is also accepted inside the current symbol. A different nonzero tag is accepted
only at a symbol boundary and selects the new symbol's rate. Orphan zero,
sparse rates, and a mid-symbol rate change are backpressured. There is no
inferred `last`, padding count, or partial-symbol flush in this interface.

The output payload sets `new_message=1` for every complete 64-bin symbol. Reset
clears the partial input words, retained rate, pilot sequence, completed-symbol
FIFO, and output epoch.

## IEEE constellation values

Complex samples are `ComplexRaw16` with signed 16-bit two's-complement I/Q
values. The integer scale is 32768. Values are rounded once to the exact raw
constants below; no BSV-specific `ComplexF` narrowing or implicit saturation is
part of the contract.

| modulation | bits | I/Q raw values |
|---|---|---|
| BPSK | `0` / `1` | `-32768` / `+32767` |
| QPSK | `00,01,10,11` | `(-23170,-23170)`, `(-23170,+23170)`, `(+23170,-23170)`, `(+23170,+23170)` |
| 16-QAM | each two-bit Gray level | `00 -> -31086`, `01 -> -10362`, `11 -> +10362`, `10 -> +31086` |

R1 consumes one bit per data carrier, R2 consumes two bits per carrier, and R4
consumes four bits per carrier. Within a chunk, the first source bit is the
most-significant representation bit, matching the existing interleaver and
project source order. For R2 the first two bits form `(I,Q)`; for R4 the first
two bits form I and the second two form Q.

## Pilot polarity

The four pilots use `[p, p, p, -p]` at indices `[11,25,39,53]`. `p` is the
current bit of the IEEE 127-bit pilot polarity sequence. The state is initialized
to the standard aligned sequence represented by:

```text
0x78869B7EEC8A4A79958C25EA82D72380  (127 bits, MSB first)
```

The sequence advances once when a complete symbol is committed, not when a
partial chunk is accepted or when output is merely stalled. Pilot values use
the same signed BPSK raw values as data.

## Historical BSV discrepancies

`Mapper.bsv` declares `curM` with `mkRegU()` and never assigns it. Its R2/R4
paths therefore do not define the required earlier chunks. The source also
uses a historical `ComplexF` representation whose operator narrowing is not a
backend-independent numerical contract. The ZLang implementation uses explicit
word registers, exact IEEE constellations, and one post-group symbol commit.

The old BSV's 48-to-64 placement and `[p,p,p,-p]` pilot structure agree with
the IEEE layout and are retained. Any future discrepancy is resolved in favor
of this document, not by changing the IEEE oracle to match generated BSV.

## Scope boundary

This slice does not implement IFFT, cyclic extension integration, packet
padding, puncturing, tail bits, or full transmitter framing. It adds no syntax,
formal observation family, protocol adapter, or backend-specific mapper path.
