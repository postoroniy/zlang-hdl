# IEEE 802.11a transmitter validation project

This standalone ZLang project validates a bounded IEEE 802.11a-derived OFDM
transmit path for the 6, 12, and 24 Mbit/s profiles. It is a real-design
compiler and backend exercise, not a production radio or a claim of complete
802.11 conformance.

The implementation is ordinary ZLang source:

```text
command + PSDU
  -> controller / SIGNAL and DATA framing
  -> scrambler
  -> convolutional encoder
  -> interleaver
  -> constellation mapper and pilots
  -> inverse radix-2 DIF-SDF IFFT64
  -> natural-order reorder
  -> 16-sample cyclic prefix
  -> ready/valid complex samples
```

`Ieee80211aTransmitter` is the stable project top. Both Clash and direct
SystemVerilog are generated from the same typed ZLang hierarchy.

## Source layout

| Source | Responsibility |
|---|---|
| `src/data_types.zl` | rates, metadata, raw boundaries, and shared aliases |
| `src/controller.zl` | SIGNAL construction and IEEE packet framing |
| `src/scrambler.zl` | packet-epoch data scrambling |
| `src/conv_encoder.zl` | K=7 convolutional helpers and streaming encoder |
| `src/interleaver.zl` | IEEE 48/96/192-bit permutations and grouping |
| `src/mapper.zl` | BPSK/QPSK/16-QAM, pilots, and bin serialization |
| `src/ifft_library.zl` | exact widened DIF-SDF stages and final quantization |
| `src/cyclic_extender.zl` | bit-reversal reorder and 80-sample cyclic prefix |
| `src/ifft.zl` | framed IFFT composition and metadata alignment |
| `src/transmitter.zl` | complete `Ieee80211aTransmitter` hierarchy |

The names follow the logical units of the audited Bluespec design, but the
behavior follows IEEE 802.11 where the historical source is demonstrably
incorrect. Shared stream, coding, complex-number, and FFT helpers come from the
compiler-shipped `std` library rather than project-local utility copies.

The ten units currently total **1,681 lines**. They intentionally use inferred
immutable locals, struct pun/update and destructuring, generated vectors,
direct LSB-zero packed-bit selection, packed slices, nominal enum switches, and
concise child connections. Generic IFFT stages derive index widths with
declaration-ordered `index_width(...)` defaults; static permutations use
half-open vector ranges and mathematical compile-time iterator arithmetic;
true resize boundaries use contextual `truncate(expr)` / `extend(expr)`.
Raw `bitcast` remains only where the design changes representation (for example
between packed words and aggregate storage layouts); transmitted-bit selection
uses packed `bits<N>[index]` directly. Fixed-point raw and quantization
boundaries remain explicit numerical intent.

## Numerical and transaction contract

- SIGNAL rate codes are `1101`, `0101`, and `1001` for 6/12/24 Mbit/s.
- DATA consists of 16 zero SERVICE bits, PSDU bytes LSB-first, six encoder-tail
  zeros after scrambling, and PAD to `N_DBPS` 24/48/96.
- The mapper emits natural-order 64-bin Q1.15 complex frames with the standard
  pilot positions and a per-packet pilot epoch.
- IFFT stages use Q2.22 inverse twiddles and exact widened arithmetic. The only
  lossy boundary is the final nearest-even, saturating conversion to Q1.15
  after exact `1/64` scaling.
- Reorder and cyclic extension emit 80 samples: indices 48..63 followed by
  0..63.
- Ready/valid backpressure holds payload and metadata. Reset starts a new packet
  epoch and discards incomplete work.

The detailed numerical contract is in
[the IFFT64 contract](../../../docs/80211a-ifft64-contract.md). Full conversion
evidence and historical BSV discrepancies are retained in
[the validation report](../../../docs/80211a-transmitter-validation.md).

## Build and check

From this project directory:

```sh
# Semantic check of the complete hierarchy
../../../.venv/bin/zlangc src/transmitter.zl \
  --project zlang.toml --top Ieee80211aTransmitter --check

# Direct SystemVerilog
../../../.venv/bin/zlangc src/transmitter.zl \
  --project zlang.toml --top Ieee80211aTransmitter \
  --systemverilog build/Ieee80211aTransmitter.sv

# Clash-generated Verilog plus Verilator lint
../../../.venv/bin/zlangc src/transmitter.zl \
  --project zlang.toml --top Ieee80211aTransmitter \
  --verilog-dir build/Ieee80211aTransmitter-clash \
  --verilator-lint
```

Useful leaf checks:

```sh
../../../.venv/bin/zlangc src/controller.zl \
  --project zlang.toml --top IeeeDataFramer24 --check

../../../.venv/bin/zlangc src/interleaver.zl \
  --project zlang.toml --top IeeePacketEncoderInterleaver24 --check

../../../.venv/bin/zlangc src/mapper.zl \
  --project zlang.toml --top IeeePacketMapper64 --check

../../../.venv/bin/zlangc src/ifft.zl \
  --project zlang.toml --top IeeeFramedIFFT64Raw --check
```

Generated RTL, ROM companions, compiler workspaces, and test output belong in
ignored build or temporary directories and must not be committed.

## Project metadata and attribution

`zlang.toml` declares this dependency-free project rooted at `src/`.
`zlang.lock` pins the external dependency closure; compiler-shipped `std.*`
modules are resolved separately.

The conversion was informed by the MIT-licensed
`freecores/bluespec-80211atransmitter` source at audited commit
`d654bfd4c2ffabc61437c131770beff58dc55b04`, originally copyright 2006
Nirav Dave. See [NOTICE](NOTICE) and [LICENSE](LICENSE). Python and historical
BSV models are independent verification oracles only; they are not executable
parts of the ZLang design.
