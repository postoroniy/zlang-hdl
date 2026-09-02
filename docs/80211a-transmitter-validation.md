# 802.11a transmitter real-design validation

## Purpose and reference boundary

This unnumbered product-validation project ports the architecture of Nirav
Dave's MIT-licensed Bluespec 802.11a transmitter into ordinary ZLang source.
The audited reference is commit
`d654bfd4c2ffabc61437c131770beff58dc55b04` in
`freecores/bluespec-80211atransmitter`. It contains 15 BSV files (2,257 raw
lines) and the following data path:

```text
controller -> scrambler -> convolutional encoder -> interleaver
           -> mapper -> IFFT64 -> cyclic extender
```

The historical design implements only the 6, 12, and 24 Mbit/s subset. This
project is therefore a language/backend validation design, not a claim of
complete 802.11a compliance.

The reference has no checked-in testbench or golden vectors and contains
observable ambiguities. It is compiled and compared where behavior is clear,
but it is not the sole semantic oracle. Later slices require independent
integer stage models before accepting compatibility or standards-facing
behavior.

The accepted executable project is consolidated into ten canonical units:

```text
data_types.zhl -> controller.zhl -> scrambler.zhl -> conv_encoder.zhl
              -> interleaver.zhl -> mapper.zhl -> ifft_library.zhl
              -> cyclic_extender.zhl -> ifft.zhl -> transmitter.zhl
```

`Ieee80211aTransmitter` is the stable external top. Earlier compatibility
modules and temporary implementation-prefixed files mentioned below are
historical findings only; they are not runnable source paths or alternate
semantic authorities.

The canonical units use existing concise instance, connection, guarded-rule,
inferred-local, struct, and vector forms. Together they remain below the
2,000-line source target while preserving the physical hierarchy and all
explicit ABI, enum/raw, fixed-point, quantization, and narrowing boundaries.

### Canonical source-consolidation acceptance

The original consolidation acceptance contained exactly those ten `.zhl` files
and 1,744 source lines. The later source-only audits described below reduced
the accepted checkpoint to 1,673 lines. The current concise-lowering working
tree contains **1,681 lines** after making generic FFT stage widths and
declaration-ordered defaults explicit; final repository acceptance for that
follow-up is pending. The canonical ten-file set is unchanged.
There are no executable legacy/implementation-prefixed compatibility
paths or module names. The direct-SystemVerilog public header and the
backend-independent `TopPhysicalABI` retain the exact 25 leaves of the prior
`Ieee80211aTransmitter`, including clock/reset and every flattened ready/valid
payload member.

The canonical suite contains 144 tests. It retains all 6/12/24-Mbit/s packet,
SERVICE/tail/PAD, interleaver, constellation, IFFT, reorder, cyclic-prefix,
stall, simultaneous transfer, and reset checks, plus independent randomized
96-word scrambler and 64-word II=1 simulation coverage. The current example
registry is 80 files / 165 module roots / 150 standalone / 15 child-template /
zero unsupported; every standalone direct-SV root passes strict Verilator.

At original consolidation acceptance, real simulator, Clash 1.11,
direct-SV/Verilator, clean-wheel resolution,
BackendArtifact round-trip, source bindings, and applicable existing M35
property/solver regressions pass. Two unchanged-snapshot complete repository
runs report **2761 passed, 1 documented opt-in skip** in 822.24 s and 682.96 s.
The historical second skip belonged to the deleted symbol-generator fixture;
the canonical full-top Clash replay executes and passes instead.

Four compiler bugs exposed by consolidation were repaired generically and have
non-Wi-Fi regressions: named specializations on concise children, mixed
ordinary/generic recursion analysis, dependency-topological direct-SV function
temporaries, and resolved callable-graph deduplication. Source relocation
recaptures source/dependency-sensitive artifact and eager-result identities;
the canonical IR schema is unchanged.

The release's machine-readable zero-skip minimum is recorded in
[`release/status.json`](../release/status.json), and FFT512 replay is part of
the default suite; see [Current language status](current-language-status.md).

### Concise source audit — 2026-08-29

The canonical project was reviewed again against the current language surface.
The accepted source now uses one raw vector view per indexed packed value,
`reshape` for logical collection flattening, exact slices for packed fields,
generated next-state vectors shared by bypass and register writes, struct
destructuring/update, `repeat`, nominal enum switches, and direct signed
constellation literals. The scrambler's 24 handwritten feedback locals are now
one data-driven tap table plus `generate`. IEEE bit order, fixed-point raw and
quantization boundaries, hierarchy, public ABI, stalls, and reset epochs remain
explicit.

This reduces the live ten-file project from 1,737 lines at audit entry to
**1,681 lines** (and by 63 lines from the historical 1,744-line consolidation
record). Existing packet/numerical oracles and simulator, Clash, direct-SV, and
strict-Verilator witnesses validate the refactor; the source-sensitive eager
CompilationResult digest is intentionally recaptured.

One general compiler limitation prevented a further safe reduction. A single
generic `ieee_first<N,W>` permutation specializes and simulates correctly in
isolation, but hierarchy-wide callable publication currently rebinds its
declaration provenance to importing child units. Identical specialization IDs
therefore appear with conflicting declaration metadata and Clash correctly
fails closed. The executable source retains the three `48/96/192` wrappers
until callable declaration ownership is fixed generically; no Wi-Fi or backend
name workaround was introduced.

### Packed-bit selection audit — 2026-08-30

The remaining `bitcast<vec<N,bit>>` expressions were representation-only views
needed because packed `bits<N>` values previously had slices but no single-bit
selection. ZLang now accepts compile-time-proven `raw[index]`, returns `bit`,
and uses conventional LSB-zero packed numbering. It lowers to the existing
`Slice` plus exact-width `Bitcast` IR, so no new canonical/backend node or
hardware operation was introduced.

All Wi-Fi vector bit views were removed. MSB-first transmitted sequences now
show their reversal explicitly (`word[47-i]`, `word[31-i]`, and
`word[15-i]`), while fixed positions use direct packed numbering such as
`pilot_state[0]`. Generated logical collections are packed once into their
declared `bits<W>` result. The project now contains **1,673 lines**. The
generic packing/backend witness and complete IEEE simulator/Clash/direct-SV/
Verilator gate verify the LSB/MSB convention and frozen packet behavior.

### Exact concise-lowering follow-up — 2026-09-01

The current source uses contextual `truncate(expr)` / `extend(expr)`, static
half-open vector ranges, mathematical compile-time iterator arithmetic,
declaration-ordered `index_width(...)` defaults, and a concise priority chain.
These forms lower to the same typed conversions, generated indices, parameter
values, and ordered rule-priority pairs as their verbose spellings. Focused
semantic/canonical and strict direct-SV/Verilator checks pass; the final two-run
repository gate is still pending, so the accepted regression baseline remains
the one recorded in [Current language status](current-language-status.md).

The controller's existing rule exclusivity/priority M35 properties now bind to
formal-only accepted-fire observations derived from the resolved schedule.
Production direct-SV and Clash RTL text/ABI/hashes remain unaffected. This is a
closure of the existing rule family, not a new Wi-Fi-specific property.

## First slice: cyclic extender

The first conversion slice, now retained only as historical evidence, preserved
the BSV transaction shape and mapping:

```text
input  : rv<ComplexRawMessage<64>>
output : rv<ComplexRawMessage<81>>

output.new_message = 1
output.data = input.data[63] ++ input.data[0:64] ++ input.data[0:16]
```

The 81-element result is deliberate compatibility behavior. A conventional
64-point OFDM symbol plus a 16-sample cyclic prefix contains 80 samples; a
standards-facing correction is a later explicit decision, never a silent port
change.

`ComplexRaw16 { i:s16, q:s16 }` preserves the payload bit shape without
claiming that the legacy `ComplexF` arithmetic equals ZLang
`Complex<fixed<...>>`. `ComplexRawMessage<N>` also validates value-parameterized
generic structs in a real protocol payload.

The FIFO retains the original 2049-bit 64-sample transaction. The 2593-bit
81-sample value is constructed on the read side. Storing the widened output
would add 544 state bits per FIFO entry and invalidate later area comparison.
Accepted ready/valid transfers alone push and pop the depth-two FIFO; output
payload remains stable while stalled and reset starts a new empty epoch.

## Concrete compiler findings

The slice found and repaired three generic correctness defects:

1. The legacy direct-SV FIFO emitter assumed every FIFO was a same-width
   ready/valid pass-through. It now renders typed read-side assignments and
   projections over an explicitly published `fifo.front`, while preserving
   exact storage width and controls.
2. Direct-SV correctly mangled legal ZLang ports named `input` and `output`, but
   BackendArtifact publication validated the unmangled spelling. Protocol leaf
   locators now use the same backend identifier mapping as RTL emission.
3. The resize token `extend` captured the prefix of an ordinary identifier such
   as `extended`. The lexer now requires an identifier boundary and retains all
   existing `extend<N>`/`truncate<N>` syntax.

At this first-slice boundary a `generate` iterator could not be forwarded as an
explicit function value-specialization argument such as `helper<I=i>(...)`.
The later generic-expression work closed that limitation without adding a
Wi-Fi construct: compile-time iterators now supply ordinary generic value
arguments. This paragraph is retained as historical evidence for that generic
repair; no state-carrying `scan` operation was added.

## Validation evidence

An independent index/integer oracle checks all 81 positions. Simulator and
both RTL backends additionally exercise:

- reset-empty behavior;
- an accepted transaction held for multiple stalled cycles;
- exact full-width payload stability;
- retirement without duplication;
- a buffered pre-reset transaction discarded by reset;
- the first clean post-reset transaction.

Real BSC `2025.01.1-110-g090e74c4` compiled the original
`mkCyclicExtender`. Its generated RTL plus the BSC `FIFOL1` primitive passed a
full-width Verilator transaction test against the same logical 64-to-81 index
oracle. ZLang direct SV and real Clash 1.11 independently pass the equivalent
test.

Current size evidence (comments included) is:

| representation | lines | bytes |
|---|---:|---:|
| ZLang cyclic-extender source | 58 | 1,903 |
| direct SystemVerilog | 53 | 5,037 |
| generated Clash source | 79 | 10,390 |
| Clash-generated Verilog | 1,045 | 38,377 |
| BSV-generated module only | 103 | 2,541 |
| BSV module plus `FIFOL1` primitive | 235 | 5,822 |

These are readability/build-size observations, not QoR. No LUT/FF/Fmax claim is
made without identical synthesis and implementation constraints.

The existing M35 generator produces seven connected FIFO/ready-valid safety
properties, and every required observation has a physical direct-SV binding.
Real SBY/Yosys/Z3 execution of this unusually wide aggregate timed out at the
fixed 120-second limit even at BMC depth two. The result is correctly
`unknown`, never `bounded_pass`; the timeout handler was repaired to normalize
mixed byte/text partial logs rather than raising `TypeError`. This is formal
scalability evidence, not permission to expand formal infrastructure.

The accepted validation totals are **5 bounded project tests**, **107 focused
compiler/backend tests**, and **1647 passed with 1 explicit opt-in FFT512 replay
skip** in the complete eight-worker repository regression. Python `compileall`,
strict Verilator lint, and staged/unstaged `git diff --check` also pass. The
direct-SV example corpus now contains 66 source files and 115 module roots: 107
standalone supported and 8 child/template-only, with no unsupported roots.

## Second slice: corrected-intent Scrambler24

The next compatibility slice, now retired from the executable project, used a
`RateWord24 { rate:u3, data:bits<24> }` payload. Rate tags `1`, `2`, and `4` start a
packet, while rate zero continues the preceding packet. The historical BSV is
not a behavioral oracle for continuation: `seqR` is an uninitialized `mkRegU`,
is read by continuation words, and is never assigned. ZLang therefore freezes
the corrected numerical intent explicitly rather than reproducing undefined
state:

```text
seed = 0b1001011
for i = 0 .. 23:                 # data[0] first
    feedback = state[0] xor state[3]
    output[i] = input[i] xor feedback
    state = (feedback << 6) | (state >> 1)
```

A nonzero rate selects the seed before processing that word. Rate zero starts
from the state committed by the preceding accepted word. The independent
integer oracle freezes these anchors:

| transfer | input | output | next state |
|---|---:|---:|---:|
| packet start | `0x000000` | `0x1fc762` | `0x0f` |
| continuation | `0x000000` | `0x1269ee` | `0x09` |

The complete reseed/continuation sequence also checks outputs `0x1fc762`,
`0x1269ee`, `0xbcb8de`, `0x1fc762`, and `0x1269ee` for inputs tagged
`(1,0)`, `(0,0)`, `(0,0x123456)`, `(4,0)`, and `(0,0)` respectively.

### Ordinary ZLang implementation and transaction semantics

The source uses one `u7` register, the representation-bit `parity` intrinsic, and a
compile-time table of masks representing the exact 24-step linear recurrence.
The scrambled word and preserved rate tag enter a depth-two FIFO. No scrambler,
tap, packet-rate, or recurrence behavior is implemented in either backend.

`input.ready` comes from FIFO capacity. Only `input.valid && input.ready`
enqueues a word and commits the corresponding next seven-bit state.
`output.valid`, `output.payload`, and `output.ready` use the existing FIFO
ready/valid semantics: push and pop may coincide when capacity is already
available, while a full FIFO backpressures input until retirement commits.
Downstream stall keeps the FIFO head and state stable. Synchronous reset clears
buffered output, restores state `0x4b`, discards the preceding protocol epoch,
and makes a subsequent rate-zero word start from the seed.

A direct source chain `s0 -> s1 -> ... -> s24` was tested first. Semantic
checking took about 30 seconds, and direct emission remained at 100% CPU after
more than 76 seconds before being interrupted. Because named locals were
re-expanded rather than shared, the expression grew exponentially; there is no
meaningful generated-size number for that failed prototype. The equivalent
parity-mask source restores direct emission to 1.29 seconds. This is concrete
expression sharing/materialization scalability debt, not a numerical,
ready/valid, or state-semantics blocker, and it does not justify adding `scan`
syntax in this slice.

### Generic backend findings

The slice repaired two direct-SystemVerilog correctness gaps without adding
802.11a-specific dispatch:

1. Selecting a slice from a compound packed expression now uses an exact-sized
   shift/materialization path instead of emitting an illegal postfix select on
   a cast expression.
2. The SystemVerilog reserved word `sequence` now participates in the generic
   identifier-mangling policy, so legal ZLang identifiers cannot corrupt RTL.

Both fixes are covered by minimized backend regressions and remain valid under
strict Verilator parsing.

### Validation evidence

The independent recurrence covers fixed anchors, reseeding, continuation, and
deterministic random multiword sequences. The semantic simulator and both RTL
backends cover sustained II=1 traffic, source gaps, a full FIFO, propagated
backpressure, held-payload stability, simultaneous FIFO activity, reseeding,
reset while output is buffered, and the first post-reset continuation. Direct
SV and real Clash 1.11 both pass strict Verilator lint and behavioral simulation
with bit-exact agreement.

Current source/generated size evidence (comments included) is:

| representation | lines | bytes |
|---|---:|---:|
| ZLang `Scrambler24` source | 87 | 2,780 |
| direct SystemVerilog | 134 | 17,465 |
| generated Clash source | 73 | 11,123 |
| Clash-generated Verilog | 847 | 27,774 |

These measurements describe source expansion and inspectability, not synthesis
QoR.

M35 generates nine connected register/FIFO/ready-valid safety properties, with
every required direct-SV observation bound to a physical signal. Real
SBY/Yosys/Z3 BMC at depth eight exceeded the 120-second budget and is therefore
reported as structured `unknown`, never `bounded_pass` or `proven`. No property
was dropped, approximated, or converted into a success, and the result does not
authorize a new property family or broader formal infrastructure.

The original Wi-Fi slice accepted **24 focused Wi-Fi/backend/corpus tests** and
a complete eight-worker repository regression with one explicit opt-in FFT512
replay skip. The later concise bit/collection slice retains the same behavioral
oracle while replacing the custom parity helper and unnecessary `pack`/`unpack`
noise. Python `compileall`, strict Verilator lint, and staged/unstaged
`git diff --check` also pass. The direct-SV corpus now
contains 67 source files and 116 module roots: 108 standalone supported and 8
child/template-only, with no unsupported roots.

## Third slice: packet/rate Controller24

The historical `Controller24` compatibility slice was the next ordinary ZLang
conversion. Its shared transaction types were imported by both the controller
and scrambler:

```zlang
struct TxControl {
    rate   : u3
    length : u12
}

struct RateWord24 {
    rate : u3
    data : bits<24>
}
```

`Controller24` exposes ready/valid command and 24-bit MAC-data sinks plus
independent ready/valid header and tagged-data sources. It accepts a command
only while idle, when its depth-two header FIFO has capacity, for rate tags
`1`, `2`, or `4`, and with a nonzero 12-bit length. It then accepts exactly
`ceil(length / 3)` data words. The first emitted data word carries the command
rate and continuation words carry rate zero. The last partial word is not
byte-masked; unused bytes remain an upstream responsibility.

The header and data FIFOs are independent. Header backpressure does not block
MAC-data acceptance while the data FIFO has room, and data backpressure does
not prevent header retirement. State changes occur only on accepted transfers.
Synchronous reset clears both FIFOs and the active packet state, discards any
buffered or partial pre-reset transaction, and begins a fresh protocol epoch.

The conversion intentionally preserves the deterministic legacy BSV rate-code
mapping `1 -> 0b1101`, `2 -> 0b1111`, and `4 -> 0b0101`. This is a compatibility
oracle, not an 802.11a compliance claim. The latter two codes do not match the
historical datapath comments/intended 12 and 24 Mbit/s modes; standards-facing
codes would be `0b0101` and `0b1001`. The discrepancy is frozen explicitly in
`docs/80211a-controller-contract.md` rather than silently corrected.

The independent header anchors are:

| rate | length | header |
|---:|---:|---:|
| 1 | 3 | `0xd60040` |
| 2 | 6 | `0xf30000` |
| 4 | 100 | `0x513040` |

The source uses existing structs, project-local imports, pure functions,
representation bit operations, registers, rules, priority, and typed FIFOs. No
controller behavior is embedded in either backend. Current recursive discovery
contains 69 example source files and 119 module roots: 111 standalone-supported
roots and the unchanged 8 child/template-only roots, with no unsupported roots.
The Controller-specific suite is **6 passed**. The combined Wi-Fi/backend/corpus
suite is **23 passed**, including real direct-SV and Clash 1.11 behavioral
Verilator runs. The complete eight-worker repository regression is **1724
passed, 1 explicit opt-in FFT512 replay skip**. Semantic/canonical round trips,
deterministic BackendArtifact JSON, recursive state/FIFO/port bindings, and the
22 generated properties from the existing register/FIFO/ready-valid/rule/
priority families are retained without adding formal infrastructure. Twenty
properties connect through the formal-only direct-SV artifact. Two rule
scheduling properties remain explicitly non-executable because rule-fire
observations are outside the frozen backend ABI. A real depth-four
SBY/Yosys/Z3 run reaches the 120-second budget and returns structured
`unknown`, never a bounded or unbounded proof claim.

## Fourth slice: corrected-intent convolutional encoder

The historical fixed 24-bit encoder path used four ordinary ZLang modules
rather than a backend primitive. Its reusable numerical work informed the
canonical implementation now housed in `src/conv_encoder.zhl`:

| module | responsibility |
|---|---|
| `ConvolutionalEncode24` | pure K=7 numerical kernel from `(word, history)` to `(encoded, next_history)` |
| `HeaderDataJoin24` | independent depth-two header/data buffering and deterministic SIGNAL-before-data ordering |
| `ConvolutionalEncoder24` | accepted-transfer history state and a depth-two encoded-output FIFO |
| `WifiConvolutionalEncoder24` | hierarchical ready/valid composition of the join and stateful encoder |

The pure kernel processes source bits from bit 23 down to bit 0. For current bit
`x` and prior history `h1` (newest) through `h6` (oldest), it emits `(g0,g1)`
MSB-first using the historical octal polynomials 133 and 171:

```text
g0 = x xor h2 xor h3 xor h5 xor h6
g1 = x xor h1 xor h2 xor h3 xor h6
```

No narrowing or puncturing occurs: one 24-bit word always produces 48 encoded
bits. Every nonzero input tag selects zero history for that word; tag zero uses
the history committed by the previous accepted word. The encoder preserves the
tag. Representative independent anchors are:

| history mode | input | output | next history |
|---|---:|---:|---:|
| zero | `0x000000` | `0x000000000000` | `0x00` |
| zero | `0x800000` | `0xdf2c00000000` | `0x00` |
| zero | `0x000001` | `0x000000000003` | `0x20` |
| zero | `0xffffff` | `0xe68fffffffff` | `0x3f` |
| zero | `0xd60040` | `0xeba189c037cb` | `0x00` |
| zero | `0xf30000` | `0xe667fe700000` | `0x00` |
| zero | `0x1fc762` | `0x039a31ae7f44` | `0x11` |
| continuation after `0x1fc762` | `0x1269ee` | `0x31b178257540` | `0x1d` |

Encoding the final word from zero history instead yields `0x037178257540`.
That difference detects accidental reseeding of a continuation word.

### Header/data ordering and the BSC build distinction

The join accepts header and tagged-data inputs independently. When a nonzero
data word reaches the data FIFO head, it waits for a header, emits that header
as a `rate=1` ordered word without consuming the data word, and then emits the
retained data word on the next eligible transfer. Rate-zero continuations drain
without a new header. A later nonzero tag starts the same sequence again.
Sparse tags and a rate-zero word before any accepted packet start do not
progress. Output FIFO backpressure holds payload stable, and synchronous reset
clears both buffered inputs, output, committed history, and packet-start state.

The reference scheduling finding requires a precise distinction. In
`ConvEncoder.bsv`, both FIFO heads are bound before the conditional. A plain BSC
compile consequently gives the sort rule unconditional guards for both
`first()` calls, so a single-header stream can stall after emitting the header.
The historical project Makefile uses `-aggressive-conditions`; BSC then
predicates the method conditions by the branch that uses each value, allowing
the intended header/data sequence to continue. The ZLang conversion makes the
required conditions explicit. It therefore does not reproduce the plain-build
artifact, but neither does it claim that the Makefile-built BSV design has that
deadlock.

The numerical kernel, transaction join, stateful encoder, and composed top are
separately checkable. Semantic/canonical round trips retain the hierarchy and
typed ports; direct SystemVerilog and Clash use the same source-defined
polynomials and accepted-transfer state update. Existing register, FIFO,
ready/valid, rule, and priority safety-property families may be reused where
their observations bind. This slice adds no liveness property or recursive
formal observation family.

The complete encoder contract is frozen in
`docs/80211a-convolutional-encoder-contract.md`. The boundary is intentionally
narrow: tail bits, service/padding construction, puncturing, final-byte masks,
packet identity, interleaving, mapping, IFFT, and full-transmitter composition
remain outside this slice. Consequently this is not an 802.11a compliance or
complete-transmitter claim.

The accepted encoder-focused suite is **10 passed**. It covers 384 deterministic
kernel vectors, two independent 320-cycle scheduled-FIFO models, directed
invalid/orphan admission, header retention, packet continuation, stalls, reset
epochs, canonical/artifact identity, recursive bindings, existing M35 families,
and behavioral Verilator runs for both direct SV and real Clash 1.11. The
affected Wi-Fi/backend/corpus suite is **41 passed**. The resulting direct-SV
corpus is **70 files / 123 roots / 115 standalone / 8 child-only / 0
unsupported**, and the complete eight-worker regression is **1735 passed, 1
explicit opt-in FFT512 replay skip**. Of the join's existing M35 properties, 22
are executable through current bindings; two rule-fire checks retain their
explicit frozen-ABI non-executable diagnostic at this historical checkpoint.
The 2026-09-01 concise-lowering follow-up above supersedes that transport
limitation with formal-only accepted-fire observations.

## Fifth slice: deterministic legacy interleaver

The interleaver contract is frozen in
`docs/80211a-interleaver-contract.md`. Its source surface consists of three
ordinary ZLang modules:

| module | responsibility |
|---|---|
| `InterleaverBlock48` | pure, separately testable rate-one/rate-two/rate-four block permutation |
| `Interleaver48` | retained-rate accumulation, depth-two completed-block buffering, and ready/valid chunk serialization |
| `WifiEncoderInterleaver24` | hierarchical `WifiConvolutionalEncoder24 -> Interleaver48` composition |

The pure mapping preserves deterministic legacy numerical wiring. For rate one
it applies the historical `F1` three-way 16-bit permutation. Rate two combines
two `F1` words through the historical `F2` grouping. Rate four combines two
such halves and applies the historical per-24-bit swaps. Representative frozen
blocks are:

| rate | input words | output words |
|---:|---|---|
| 1 | `0123456789ab` | `2802872b82bf` |
| 2 | `0123456789ab`, `fedcba987654` | `3951c73951f8`, `395e07395e38` |
| 4 | `0123456789ab`, `fedcba987654`, `13579bdf0246`, `eca86420fdb9` | `3952cb3952f4`, `395d0b395d34`, `5472f1547d31`, `5782f1578d31` |

The rate-four result is intentionally a compatibility result, not an IEEE
802.11a claim. Under the encoder's frozen MSB-first bit order the legacy R1 and
R2 mappings agree with the usual two-permutation interleaver, while the legacy
R4 mapping does not. Replacing it with an IEEE-correct mapping would change the
observable numerical contract and therefore requires a separate product
decision rather than a silent cleanup.

### Corrected transaction control

The original module computes `this_rate` but derives both `new_inCnt` and the
completed-block tag from the old `cur_rate`. Immediately after reset this can
combine the rate-one SIGNAL word with the following data word and tag the
result with the preceding rate. Its count-zero case also places `?` in the
upper portion of `mapR`; current BSC-generated RTL materializes an alternating
constant there, but that value is not source semantics. The historical
`-aggressive-conditions` option affects method readiness and does not repair
either defect.

The ZLang contract instead freezes these explicit rules:

- tags `1`, `2`, and `4` select a retained packet rate and require one, two, or
  four 48-bit words per completed block;
- tag zero inherits that rate across any number of complete blocks;
- a new nonzero tag is accepted only at a block boundary;
- sparse tags, an orphan zero after reset, and a mid-block rate change do not
  transfer;
- non-final chunks may occupy the partial accumulator independently of the
  completed-block FIFO, while a completing chunk waits for FIFO capacity;
- all accumulator and padding bits are initialized, output remains stable
  under stall, and reset starts a new empty protocol epoch.

`RateWord48` does not carry `last`, a valid-bit count, or a padding count. The
bounded interleaver can therefore emit only complete 48/96/192-bit blocks. A
partial R2/R4 block waits for further rate-zero words or is discarded by
reset. Packet tail generation, padding, and partial-symbol flushing remain a
real framing boundary rather than being guessed by this module.

No compiler primitive, ROM, memory, new syntax, or new formal observation
family is required. The permutations use existing compile-time collection and
bit-layout operations; stream state uses existing registers, FIFO actions,
ready/valid semantics, and hierarchy. The applicable frozen M35 output contains
21 register, FIFO, and ready/valid properties. Ordinary non-conflicting rules
remain present in typed IR but do not fabricate rule-fire observations.

The eight focused tests pass: exact and randomized permutation oracles, a
480-cycle independent stream oracle, directed rate/capacity/reset behavior,
hierarchical encoder composition, semantic/canonical/artifact checks, direct
SV/Verilator, and real Clash 1.11/Verilator. The complete eight-worker
repository run is **1746 passed, 1 explicit opt-in FFT512 replay skip**. The
direct-SV corpus is **71 files / 126 roots / 118 standalone / 8 child-only / 0
unsupported**.

This design also exposed two generic compiler defects. Direct-SV expression
substitution and materialization formerly stopped at nested typed dataclasses
such as switch arms; both traversals now recurse through those nodes, so the
pure interleaver kernel falls from a repeated roughly 519-kilobyte assignment
to a small graph of reused temporaries. Separately, the CSR access lexer token
formerly captured the `wo` prefix of an ordinary name such as `word_bits`; its
boundary is now explicit. Both repairs have minimized non-Wi-Fi regressions.

Historical pre-materialization scalability evidence for this slice was 252
lines / 9,142 bytes of ZLang, 1,223 lines / 70,909 bytes of direct SV, and 267
lines / 1,784,310 bytes of generated Clash source. Real Clash-to-Verilog and
behavioral simulation passed, but compilation took about six minutes on the
acceptance host. These numbers describe the compiler at that slice boundary;
the generic module/callable/pure-child materialization repair documented below
supersedes them as a current backend-size result.

## Known reference issues and later slices

Before a full transmitter is accepted, later work must make explicit decisions
for reference behavior including:

- the original scrambler continuation state is undefined; `Scrambler24` now
  uses the corrected-intent recurrence above rather than bug compatibility;
- the original controller deterministically emits standards-mismatched SIGNAL
  codes for its 12 and 24 Mbit/s datapath tags; `Controller24` preserves that
  mapping as compatibility behavior rather than claiming compliance;
- a plain BSC compile of the original convolutional encoder can over-constrain
  its sort rule because both FIFO heads are named before the branch; the project
  Makefile's `-aggressive-conditions` build predicates those guards, while the
  ZLang source states the intended branch conditions directly;
- the original interleaver advances and tags blocks from stale `cur_rate` and
  partially initializes `mapR`; the ZLang contract uses the accepted effective
  rate and total initialization instead;
- the deterministic legacy rate-four interleaver differs from the usual IEEE
  permutation and is retained only as an explicit compatibility profile;
- mapper accumulation state appears to be read without being committed;
- several BSV registers use undefined reset values;
- legacy `ComplexF` rounds/narrows inside operators and is not automatically
  equivalent to ZLang's exact widened arithmetic;
- the 81-versus-80 cyclic-extension discrepancy.

After interleaver acceptance, the mapper review was resolved by the
IEEE-authoritative contract in `docs/80211a-mapper-contract.md`. Its undefined
`curM` state is not copied. Full packet padding, standards-correct rate-four
interleaving, IFFT composition, and a complete transmitter remain independent
later decisions.
The fixed 24-mask scrambler is acceptable ordinary library source for this
validation point, but its failed
naive chain is real evidence for a later, separate review of immutable
state-carrying `scan` or generic expression sharing. That review should begin
only if another real source repeats the limitation. It must not introduce scan,
new syntax, procedural locals, runtime state-array writes, BSV-specific compiler
semantics, or expanded formal machinery speculatively.

## Sixth slice: IEEE-authoritative Mapper48

The mapper contract is frozen separately in
[`docs/80211a-mapper-contract.md`](80211a-mapper-contract.md). The historical
compatibility source had a pure `MapperBlock48` kernel and a stateful
`Mapper48` ready/valid wrapper. The canonical IEEE mapper now lives in
`examples/projects/80211a_transmitter/src/mapper.zhl`.

The mapper consumes complete 48-bit chunks tagged R1/R2/R4. A zero tag
continues the retained rate, while a nonzero tag is accepted only at a symbol
boundary. The resulting 64-vector is in FFT-bin order: null bins 0--5 and
59--63, data bins 6--10, 12--24, 26--31, 33--38, 40--52, 54--58, DC/null bin
32, and pilots at 11, 25, 39, and 53. The pure kernel uses the frozen signed
raw constants for BPSK, QPSK, and Gray 16-QAM. The stateful wrapper rotates the
aligned 127-bit pilot sequence only on symbol commit, and its depth-two FIFO
holds the output payload stable under downstream stall.

The historical `Mapper.bsv` is not authoritative where it is undefined: its
`curM` is an unassigned `mkRegU`, so its R2/R4 accumulation behavior cannot
define a standards-facing result. Its agreeing carrier placement and pilot
shape are retained only as conversion evidence. IEEE 802.11a is the source of
truth; no BSV-specific `ComplexF` narrowing or implicit numerical behavior is
copied.

Validation includes deterministic randomized pure-kernel vectors across all
rates and pilot polarities, grouped R1/R2/R4 cycle traffic, rate admission,
output stall stability, reset epochs, semantic/canonical round-trips, strict
direct-SV/Verilator behavior, and real Clash 1.11 generation plus Verilator
behavior. The focused mapper suite is **7 passed**. Both backends emit the same
2049-bit packed message shape and pass strict lint. No new syntax, formal
observation family, or Wi-Fi backend dispatch was added.

The final eight-worker repository regression is **1753 passed, 1 explicit
opt-in FFT512 replay skip**; `compileall` and `git diff --check` also pass.

The next concrete blocker is composition rather than mapping arithmetic:
`RateWord48` has no packet-end, valid-bit-count, or padding metadata, and the
current bounded mapper therefore emits only complete OFDM symbols. IFFT64 and
cyclic-extension integration must be frozen against that framing boundary
before a complete transmitter claim. Puncturing, tail bits, and full packet
framing remain out of scope for this slice.

The mapper added two project module roots to the exhaustive direct-SV corpus.
At mapper-slice acceptance the matrix was **72 source files / 128 roots / 120
standalone / 8 child-only / 0 unsupported**; the mapper roots were
standalone-supported and strict-lint clean. The live corpus is maintained in
`docs/direct-systemverilog.md`.

The follow-on probe subsequently closed the generic lexer boundary,
nominal aggregate-reduction, and compile-time iterator-specialization gaps.
The accepted `sum` contract uses exact ordinary nominal `operator +`
specializations in a deterministic balanced source-order tree, retains the
high-level reduction in typed/canonical IR, and leaves the final fixed-point
quantization exactly where source writes it. `Complex` remains ordinary stdlib
code.

## IFFT64 numerical-reference elaboration boundary

The retained internal IFFT64 whole-vector elaboration reproducer exposed a
separate compiler scalability boundary:

| semantic probe | elapsed | peak RSS | backend reached |
|---|---:|---:|---|
| one inverse-DFT bin / 64 products | 23.34 s | 256,816 KiB | no; semantic check only |
| all 64 bins / 4096 products | 180.13 s, timeout | 1,161,012 KiB | no |

Eagerly cloning the nested output `generate`, inner product `generate`, nominal
reduction tree, and monomorphized operator bodies is not an acceptable interim
representation. The design-review recommendation is a backend-independent
symbolic nested functional region with lazy deterministic overload
specialization. It must preserve exact widths, source order, semantic identity,
source origins, and the single post-accumulation quantization boundary. It must
not introduce IFFT/Complex/Wi-Fi dispatch or force users to write thousands of
scalar expressions.

The initial compact-call representation still exceeded the combined gate after
**45.07 s / 813,500 KiB**, so the frozen bounded compaction fallback was
implemented. It eagerly type-checks each finite region using the existing exact
rules, then retains a generic `FunctionalRegion` and exact source-order
`ExactReductionPlan`; it is not symbolic one-pass body typing. N=64 now checks
and canonicalizes in about 7.4 seconds at about 91 MiB, and agrees lane-for-lane
with an independent Decimal/integer oracle. N=8/N=16 physical witnesses retain
the numerical/backend acceptance contract without requiring full N=64 RTL. The
current functional-expression semantics are described in
[Expressions, functions, and generics](expressions-functions-generics.md).

The reference slice is accepted: two complete eight-worker repository runs
each report **1849 passed, 1 explicit opt-in FFT512 replay skip**. Existing
scalar fixed-point M36/M38 real-solver smoke, Python `compileall`, and
staged/unstaged diff checks pass. This closes numerical-reference elaboration;
it does not implement the production streaming SDF or transmitter framing.

At closure of this bounded numerical-reference slice, a streaming inverse
radix-2 SDF was the preferred next production architecture; framing, padding,
cyclic-extension composition, and full-transmitter validation had not yet been
started. Those later slices are now complete below. The direct-SV negative
sized-constant spelling and Clash reserved leaf-port publication also exposed
by the probe were fixed as independent generic backend tooling defects. The
accepted implementation and remaining limits are recorded in this validation
report and in [Known limitations](known-limitations.md).

## Seventh slice: complete pre-IFFT symbol generator

`WifiSymbolGenerator24` is the first complete transaction hierarchy from a
packet command and 24-bit MAC words to full 64-bin frequency-domain symbols:

```text
Controller24
  +-> SIGNAL header ----------------------+
  `-> tagged data -> Scrambler24          |
                         +----------------v
                         HeaderDataJoin24
                              -> ConvolutionalEncoder24
                              -> Interleaver48
                              -> Mapper48
                              -> rv<ComplexRawMessage<64>>
```

All six stateful blocks remain physical children with nine typed ready/valid
connections. The source does not flatten state, infer RTL names, or introduce a
Wi-Fi backend path. A rate-one command of three bytes followed by `0x123456`
produces exactly two 2049-bit transfers, SIGNAL then data. Direct SystemVerilog
passes strict Verilator lint and full-payload behavioral comparison against the
independently composed controller, scrambler, encoder, interleaver, and IEEE
mapper oracles. Semantic/canonical restoration preserves the six instances and
all nine connections; BackendArtifact JSON preserves their bindings and
identities.

Composition exposed a real transaction-contract mismatch. `Interleaver48`
repeats the retained nonzero rate on every serialized chunk of one completed
R2/R4 group, while `Mapper48` formerly admitted a nonzero tag only at a symbol
boundary. The mapper now accepts a repeated tag equal to its retained rate and
continues to reject a different mid-symbol tag. This is generic source-level
ready/valid behavior, not a compiler exception. Isolated R1/R2/R4 tests cover
the repaired rule; the full composed RTL witness currently uses the clean R1
standards-facing path.

At this historical pre-IFFT slice boundary, three compiler/backend findings
were separated from language design:

1. Clash's dynamic `Vec` selector widened an otherwise exact `Index 64` to a
   machine-width RTL array index, producing strict Verilator width failures.
   Runtime vector reads now lower to a deterministic literal-index selector
   tree, preserving the typed result and eliminating the widened RTL index.
2. A reusable Clash storage child whose legal ZLang endpoint is named `data`
   could call its raw helper with the helper's private, unmangled binder rather
   than the wrapper argument. The wrapper now applies source ports in explicit
   physical order using the shared Clash identifier mapping.
3. Clash's then-current reusable ready/valid child ABI could not recursively
   emit a child that itself owned hierarchical protocol connections. It failed
   closed before invalid Haskell was published. `WifiSymbolGenerator24`
   directly instantiated the same six stateful leaves, preserving every state
   boundary. This was a Clash ABI coverage gap, not a missing ZLang construct.

The historical flattened one-level Clash source was about 1.9 MiB, and real
Clash 1.11 normalization did not finish within a three-minute bounded attempt.
This is retained as the motivating pre-fix measurement, not the current
backend result. Recursive closed ready/valid components, reachable-callable
publication, and shared typed materialization for module, callable, and pure
child bodies now reduce `WifiSymbolGenerator24` Clash source to **156,239 bytes
/ 1,059 lines**, with a longest line of **17,490 characters**. Real Clash 1.11
generation and Verilator behavioral replay now pass.

The simulator finding was likewise historical: it formerly replayed child
history on every parent cycle. Each physical child now owns persistent state
and is evaluated and committed once per cycle.

### Missing-language and non-language boundaries

The conversion produced the following historical gap snapshot. The later
language-completion section supersedes it; only the framed-stream ergonomics
and physical storage/planning rows remain open product questions.

| category | concrete gap | why it matters next |
|---|---|---|
| language/elaboration | a parent atomic rule cannot yet consume a child output in the same scheduling analysis | limits concise state-machine composition around child results |
| language/elaboration | runtime-indexed writes into aggregate state are absent | forces verbose muxed state or storage for reorder/CP buffers |
| language ergonomics | no neutral linear framed-stream abstraction beyond an ordinary `rv<struct>` | SERVICE/tail/PAD and packet epoch metadata must currently be designed ad hoc; existing `packet<T>` is arbiter-oriented |
| compact IR | pure functional regions reject `ReadyValidRef(PAYLOAD)` captures | a natural `sum(generate ... input.payload[i])` can expand to about 50 s / 741 MiB while an equivalent pure-vector child remains about 9 s / 96 MiB |
| compiler correctness | runtime `!bit` was incorrectly routed through unary-minus typing | repaired generically by lowering logical not to an exact bit comparison |
| backend | nested Clash ready/valid child components and large-expression normalization | blocks a convenient nested full-top Clash acceptance run, while direct SV and leaf Clash remain valid |
| simulator | hierarchical replay rather than persistent child state | makes long transmitter traces disproportionately expensive |
| QoR/planning | no source-to-SRL/BRAM delay mapping and no stateful elastic pipeline planning | production SDF delay lines would be functionally correct but physically weak |

Several remaining items are deliberately **not** classified as language gaps:

- exact IEEE SERVICE, PSDU valid-byte, tail-zero, PAD, and packet-epoch rules;
- corrected SIGNAL rate codes and standards-correct R4 interleaving;
- the inverse-IFFT sign, `/64` scaling location, intermediate widths,
  quantization boundaries, bit-reversed versus natural order, and reset/framing
  contract;
- replacing the historical 81-sample cyclic extension with the IEEE 80-sample
  output (`frame[(i + 48) & 63]` is already expressible);
- choosing packed registers, FIFO, SRL, or BRAM for reorder and cyclic-prefix
  storage.

Those were product/numerical/architecture decisions. The recommendation at
this boundary was to freeze IEEE framing and the IFFT numerical contract,
repair payload-only functional-region capture, then build D4/D8 witnesses
before D32 through D1. That sequence was subsequently completed without an
IFFT-specific syntax or compiler node.

Historical acceptance for this slice is **56 passed, 1 explicit full-top Clash opt-in skip**
in the focused Wi-Fi suite and **1859 passed, 2 explicit opt-in skips** in the
complete eight-worker repository regression. The exhaustive direct-SV corpus is
**73 files / 129 roots / 121 standalone / 8 child-only / 0 unsupported**.
Python `compileall` and staged/unstaged `git diff --check` pass.

## IEEE encoder/interleaver slice

The IEEE-authoritative encoder and interleaver now live in the canonical
`src/conv_encoder.zhl` and `src/interleaver.zhl` units. The retired compatibility
implementation survives only as historical evidence in this report. Its K=7
(133,171) boundary consumes DATA representation bit zero first by applying one
explicit `reverse24` before the already validated pure convolutional kernel.
History restarts on each `FrameBeat.first`, is cleared after `last`, and resets
with the protocol epoch.  Encoded pairs are represented in transmission order
from the most-significant end before interleaving.

The production interleaver performs both standard permutations for `N_CBPS`
48, 96, and 192.  The R4 correction is intentionally larger than replacing the
legacy swap mask: its first permutation must form one twelve-bit row from all
four encoded words before the `s=2` adjacent-pair permutation.  Concatenating
two independent 96-bit first permutations is not equivalent.  The one-hot R4
discriminator is `(0, 0, 0, 0x100)` rather than the legacy `(0, 4, 0, 0)`.

The stateful stage groups exactly 1/2/4 encoded words, preserves each
`FrameBeat` and `WifiWordMeta` lane, holds output under backpressure, and drops
partial groups on reset.  The concrete raw-boundary hierarchy is:

```text
IeeeDataFramer24 -> IeeeDataScrambler24
  -> IeeeConvolutionalEncoder24 -> IeeeInterleaver48
```

Independent Python models cover convolutional bit order/history, both IEEE
permutations, 6/12/24 Mbit/s symbol groups, multi-symbol packets, stalls,
back-to-back packet epochs, and reset.  The focused slice reports **12 passed**;
semantic/canonical restoration, deterministic BackendArtifact JSON, direct-SV
strict Verilator lint, and real Clash 1.11 generation plus strict lint all pass.

## Wi-Fi language-completion and production chain

This section supersedes the earlier blocker table for current work while
retaining that table as the evidence which motivated the generic fixes.

The following language/compiler gaps are now closed:

| earlier finding | implemented generic resolution |
|---|---|
| sparse externally encoded rate values | nominal `enum Name : bits<W>` plus exact `enum_encode`, `enum_valid`, and fallback-total `enum_decode` |
| verbose phase register/rule boilerplate | syntax-only `fsm` lowering into one enum register and the existing atomic-rule scheduler |
| runtime reorder-buffer writes | one range-proven dynamic element update on `reg vec<N,T>`, lowered to `VectorUpdate` |
| parent rules could not consume child results | child output signatures are published before rule typing and become read-only `InstanceOutputRef` values |
| functional regions rejected ready/valid payloads | immutable payload-only capture is supported; handshake, state, and storage remain excluded |
| hierarchical replay grew quadratically | each physical child now owns persistent simulator state and is evaluated/committed once per cycle |
| nested ready/valid Clash hierarchy failed closed | recursive closed bundled components pass every scalar/protocol dependency, including reverse `ready`, explicitly |
| very large unrelated helper publication | reachable-callable traversal and common module/callable/pure-child materialization emit each shared typed expression once; compound direct-SV dynamic-select prefixes are also named once |

These are backend-independent or generic backend/runtime corrections.  No IFFT,
Complex, rate, framing, or Wi-Fi semantic node was added, and the formal
infrastructure freeze remains intact.

The IEEE-authoritative production source is now composed as:

```text
command/PSDU
  -> IeeeDataFramer24
  -> IeeeDataScrambler24
  -> IeeeConvolutionalEncoder24
  -> IeeeInterleaver48
  -> IeeeMapper64
  -> IeeeMapperSerializer64
  -> inverse DIF-SDF D32/D16/D8/D4/D2/D1
  -> natural-order reorder
  -> cyclic prefix
```

`Ieee80211aTransmitter` exposes this hierarchy through ordinary ready/valid
ports.  The independent packet oracle composes the already-independent framer,
encoder/interleaver, mapper, exact integer IFFT, bit reversal, and CP models.
Focused semantic simulation reports **3 passed** for one-byte packets
at 6, 12, and 24 Mbit/s.  It checks the exact SIGNAL header and every emitted
Q1.15 I/Q sample and metadata field.  The corresponding canonical round trip,
deterministic direct-SV artifact, and strict Verilator lint also pass. Full
**903-cycle** packet-oracle replay passes through both direct SystemVerilog and
real Clash 1.11 generated RTL, so the complete production-transmitter file
reports **6 passed** when both tools are available.

The inverse numerical contract remains exact: Q1.15 inputs, Q2.22 inverse unit
twiddles, widened arithmetic through all six stages, multiplication by raw one
in Q1.6 after D1, and one final nearest-even/saturating Q1.15 conversion.  The
bit-reversed stage result is buffered into natural order and emits exactly
indices 48..63 followed by 0..63.  There is no intermediate narrowing.

Because the SDF recurrence is continuous, a finite packet-final symbol is
advanced by one injected all-zero 64-bin frame.  An ordered non-emitting
metadata token consumes the corresponding dummy output, so the externally
visible packet has only real samples.  Backpressure preserves payload and
metadata; reset discards the incomplete transform/reorder/metadata epoch.  The
focused production-IFFT file reports **5 passed**, including this finite-packet
flush, reset, direct-SV strict lint, and real Clash 1.11 generation/lint.

`IeeeIFFTInputStrip` is the canonical concise-FSM witness. Its `Data`/`Flush`
phase machine lowers to one `IeeeIFFTStripPhase` register and the existing
atomic-rule scheduler; the backend receives no FSM-specific node.

### Historical Wi-Fi slice acceptance evidence

At acceptance of this Wi-Fi slice, the exhaustive direct-SV registry contained
**85 files / 176 roots / 154
standalone / 22 child or template / 0 unsupported**, and every standalone root
passes strict Verilator lint. Two complete eight-worker regressions report
**2078 passed, 2 explicit opt-in skips** in **666.43 s** and **546.57 s**.
Python `compileall` and staged/unstaged `git diff --check` pass.

Both backends were emitted from the same selected semantic design with all six
ROM companions. The bounded artifact evidence is:

| backend | generated artifact | Yosys 0.68 `synth_xilinx` | Vivado 2024.2 routed, `xc7z030ffg676-1`, 10 ns |
|---|---|---|---|
| direct SV | 219,146 bytes / 2,627 lines; 1.213 s generation | 16,266 LUT / 4,939 FF / 236 DSP / 0 BRAM | 18,669 LUT / 4,834 FF / 120 DSP / 0 BRAM; WNS -4.354 ns; critical-delay proxy 14.354 ns; Fmax proxy 69.667 MHz |
| Clash | 636,282 bytes / 16,643 lines across 23 RTL files; 42.869 s generation | timed out after 900.030 s; no mapped result is claimed | 25,980 LUT / 21,097 FF / 120 DSP / 2 BRAM; WNS -4.790 ns; critical-delay proxy 14.790 ns; Fmax proxy 67.613 MHz |

Reproduce the bounded run from the repository root with:

```bash
.venv/bin/python tools/80211a_transmitter_backend_qor.py \
  --backend both \
  --output /tmp/zlang-80211a-qor \
  --part xc7z030ffg676-1 --period-ns 10 \
  --yosys-timeout 900 --vivado-timeout 3600
```

The JSON result records the top-source hash, dependency-closure identity,
BackendArtifact hashes, all six companion hashes, generated RTL paths, tool
versions, bounded process statuses, and metrics.

Neither backend meets the 10 ns constraint in this first deliberately
dual-bank architecture. These measurements are reproducible evidence, not a
QoR pass/fail gate, an II=1 physical-throughput proof, or a reason to promote
one backend over the other. The Clash Yosys timeout is recorded as a timeout,
never as a mapped result.

The remaining known product/architecture limitations are the deliberately
dual-bank exact SDF stage, a single-buffered 64-entry reorder, lack of automatic
SRL/BRAM selection, and no stateful elastic `pipeline(auto)`.  These do not
change the functional source contract and must not be hidden by intermediate
quantization or backend-specific logic.
