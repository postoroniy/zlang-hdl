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

`Ieee80211aTransmitter` is the stable project top. Production
SystemVerilog is generated from the backend-independent typed ZLang hierarchy.

## Source layout

| Source | Responsibility |
|---|---|
| `src/data_types.zhl` | declaration-only nominal rates, one typed raw-rate decoder, shared symbol-boundary policy, metadata, and aliases |
| `src/controller.zhl` | SIGNAL construction, FSM-owned packet framing, and packet-epoch scrambling |
| `src/formal.zhl` | bounded formal wrapper for production SIGNAL encoding |
| `src/interleaver.zhl` | K=7 convolutional encoding plus IEEE 48/96/192-bit interleaving and grouping |
| `src/mapper.zhl` | BPSK/QPSK/16-QAM, pilots, and bin serialization |
| `src/ifft_library.zhl` | exact widened DIF-SDF stages and final quantization |
| `src/ifft.zhl` | framed IFFT composition, bit-reversal reorder, cyclic prefix, and metadata alignment |
| `src/transmitter.zhl` | complete `Ieee80211aTransmitter` hierarchy |
| `simulation/demo-events.jsonl` | compact one-byte stimulus with bounded repeated ready/stall intervals |
| `zlang.toml` / `zlang.lock` | project identity, source root, and exact dependency closure |

The names follow the logical units of the audited Bluespec design, but the
behavior follows IEEE 802.11 where the historical source is demonstrably
incorrect. Shared stream, coding, complex-number, and FFT helpers come from the
compiler-shipped `std` library rather than project-local utility copies.

The production source intentionally uses ZLang's nominal enums, immutable
typed products, inferred locals, struct pun/update and destructuring,
ready/valid protocols, FIFOs, atomic prioritized rules, FSMs, generated
vectors, ROMs, generic functions and modules, compile-time arithmetic, direct
LSB-zero packed indexing, packed slices, and concise child connections. One
`wifi_decode_rate` function converts the sparse raw IEEE field into a
`WifiDecodedRate` product; one `wifi_symbol_slot_last` function owns the shared
controller/interleaver/mapper symbol-boundary policy. The design therefore does
not reimplement those semantic decisions at each stage. The data-types source
is a declaration unit rather than a dummy hardware module, and the framer uses
an explicit `Idle`/`Active` FSM whose transitions atomically own admission,
payload collection, and tail/PAD flushing.

Generic IFFT stages derive index widths with declaration-ordered
`index_width(...)` defaults; static permutations use half-open vector ranges
and mathematical compile-time iterator arithmetic; true resize boundaries use
contextual `truncate(expr)` / `extend(expr)`. Raw `bitcast` remains only where
the design changes representation (for example between packed words and
aggregate storage layouts); transmitted-bit selection uses packed
`bits<N>[index]` directly. Fixed-point raw and nearest-even/saturating
quantization boundaries remain explicit numerical intent. Features without a
real ownership role here—CSR buses, tagged unions, temporal multiplier sharing,
and registered scalar outputs—are deliberately not inserted as showcase-only
logic.

The eight source units follow hardware ownership rather than placing every
small helper module in a separate file. Within them, inferred immutable locals,
struct destructuring and punning, `with` updates, generated vectors, concise
child connections, and FSM transitions keep the dataflow readable. Comments at
each non-obvious concise form explain the elaborated hardware meaning, so the
example remains useful as a language guide rather than relying on shorthand
without context.

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

The IFFT64 numerical contract and end-to-end conversion are exercised by the
project's compiler, simulation, and direct-SystemVerilog validation tests.

## Simulation, waveforms, and formal

The project uses the ordinary ZLang CLI directly; there is no project-specific
Makefile or Python task runner. From this directory, activate the checkout-local
environment and validate the checked-in dependency lock:

```sh
source ../../../.venv/bin/activate
zlang lock update
```

All commands below discover `zlang.toml` and the matching `zlang.lock`
automatically from the source path. The simulation schedule is eight JSONL
records expanded deterministically to 400 clock events through the bounded
`repeat` field. It sends one BPSK byte (`0x55`) and applies two downstream
stalls. The repository regression requires exactly one SIGNAL transfer
(`0x00002d`) and one 160-sample, two-symbol time-domain packet with preserved
first/last boundaries.

Run native simulation and compare every event against generated Direct-SV in
Verilator:

```sh
mkdir -p build
zlang sim src/transmitter.zhl --top Ieee80211aTransmitter \
  --events simulation/demo-events.jsonl \
  --compare-with verilator --json > build/simulation.json
```

Generate a selective VCD from the same schedule. Physical `clk` and `rst` are
always included, even when data/state signals are selected explicitly:

```sh
zlang sim src/transmitter.zhl --top Ieee80211aTransmitter \
  --events simulation/demo-events.jsonl \
  --trace build/wifi80211a.vcd \
  --trace-signal '$zlang_protocol:command:valid' \
  --trace-signal '$zlang_protocol:psdu:valid' \
  --trace-signal '$zlang_protocol:output:payload' \
  --trace-signal '$zlang_protocol:output:valid' \
  --trace-signal '$zlang_protocol:output:ready' \
  --trace-signal 'packet_mapper.encoder_interleaver.framer.remaining_bytes' \
  --json > build/simulation.json
gtkwave build/wifi80211a.vcd
```

Run the bounded formal wrapper directly through the normal compiler command:

```sh
zlang src/formal.zhl --top Ieee80211aSignalFormal \
  --verify --verify-require proven --formal-depth 8 --formal-timeout 45 \
  --verification-work-dir build/formal-work \
  --verification-report build/formal-report.json \
  --verification-format json > build/formal-console.json
```

This proves the production SIGNAL rate-code mapping and input-validity
conditions and witnesses a valid one-byte BPSK request through a real
Yosys/SymbiYosys/SMT run over generated Direct SystemVerilog. The current
executable formal subset does not lower the parity reduction used by the full
header, so parity and complete packet/datapath behavior remain covered by
simulation and native-versus-Verilator differential tests. It is not a proof
of complete IEEE 802.11 conformance or eventual packet delivery.

## Build and check

From this project directory:

```sh
# Semantic check of the complete hierarchy
zlang src/transmitter.zhl --top Ieee80211aTransmitter \
  --profile portable --check

# Direct SystemVerilog plus the profile-owned portable clock constraint
zlang src/transmitter.zhl --top Ieee80211aTransmitter \
  --profile portable \
  --systemverilog build/Ieee80211aTransmitter.sv \
  --constraints-sdc build/Ieee80211aTransmitter.sdc

# Production direct-SystemVerilog plus Verilator lint
zlang src/transmitter.zhl --top Ieee80211aTransmitter \
  --profile portable \
  --systemverilog build/Ieee80211aTransmitter.sv --verilator-lint
```

Useful leaf checks:

```sh
zlang src/controller.zhl --top IeeeDataFramer24 --check

zlang src/interleaver.zhl --top IeeePacketEncoderInterleaver24 --check

zlang src/mapper.zhl --top IeeePacketMapper64 --check

zlang src/ifft.zhl --top IeeeFramedIFFT64Raw --check
```

Generated RTL, ROM companions, compiler workspaces, and test output belong in
ignored build or temporary directories and must not be committed.

## Project metadata and attribution

`zlang.toml` declares this dependency-free project rooted at `src/` and a
target-neutral `portable` Direct-SV profile. `zlang.lock` pins the external
dependency closure and is checked automatically for every command;
compiler-shipped `std.*` modules are resolved separately.

The conversion was informed by the MIT-licensed
`freecores/bluespec-80211atransmitter` source at audited commit
`d654bfd4c2ffabc61437c131770beff58dc55b04`, originally copyright 2006
Nirav Dave. The original material and the ZLang port and modifications in this
project subtree are distributed under that MIT license; contributions to the
subtree follow the same license unless a file states otherwise. See
[NOTICE](NOTICE) and [LICENSE](LICENSE). Python and historical BSV models are
independent verification oracles only; they are not executable parts of the
ZLang design.
