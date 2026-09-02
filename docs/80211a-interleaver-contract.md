# 802.11a interleaver conversion contract

Status: historical compatibility evidence. The executable compatibility module
has been retired; `src/interleaver.zhl` implements the IEEE permutations.

This note separates deterministic legacy numerical wiring from broken or
undefined transaction behavior in the historical Bluespec interleaver. The
result is a source-conversion and language-validation contract, not an IEEE
802.11a compliance claim.

## Block and stream contract

The interleaver consumes and emits `RateWord48` ready/valid transactions.
Nonzero tags select the retained packet rate:

| tag | historical modulation profile | input/output words per block |
|---:|---|---:|
| 1 | BPSK | 1 |
| 2 | QPSK | 2 |
| 4 | 16-QAM | 4 |

Tag zero inherits the last accepted nonzero rate across any number of complete
blocks. Every emitted word repeats that effective nonzero rate, matching the
historical mapper-facing ABI. A new nonzero tag is accepted only when no block
is partially accumulated. Sparse tags, an orphan zero after reset, and a
mid-block nonzero rate change do not transfer.

There is no `last`, padding count, or partial-symbol flush in `RateWord48`.
Consequently only complete 48/96/192-bit blocks are emitted. A partial R2/R4
block remains buffered until enough rate-zero words arrive or reset starts a
new protocol epoch.

Completed blocks use a depth-two FIFO and a separate output chunk counter. A
non-final input chunk may be accepted while that FIFO is full; the completing
chunk waits for FIFO capacity. Output data and tag remain stable under stall.
Reset discards retained rate, partial input, completed blocks, and a partly
emitted block.

## Frozen legacy numerical wiring

Input words are interpreted MSB first. Define `F1` for one 48-bit word as the
following output-bit sequence for `r = 0..15`:

```text
word[47-r], word[31-r], word[15-r]
```

`F2(current, previous)` alternates each three-bit group of `F1(previous)` with
the corresponding three-bit group of `F1(current)`. The selected blocks are:

```text
R1: F1(w0)
R2: F2(w1, F1(w0))
R4: switch24(F2(w1, F1(w0)) || F2(w3, F1(w2)))
```

`switch24` partitions the 192-bit value into eight consecutive 24-bit slices.
Within every slice it swaps MSB-indexed positions 14 with 15 and 20 with 21;
all other positions remain unchanged. Unlike the historical source, unused
storage is zero-initialized and cannot become observable.

Frozen anchors are:

| rate | input words | output words |
|---:|---|---|
| 1 | `0123456789ab` | `2802872b82bf` |
| 2 | `0123456789ab`, `fedcba987654` | `3951c73951f8`, `395e07395e38` |
| 4 | `0123456789ab`, `fedcba987654`, `13579bdf0246`, `eca86420fdb9` | `3952cb3952f4`, `395d0b395d34`, `5472f1547d31`, `5782f1578d31` |

## Deliberate standards distinction

For N=48/96, the historical wiring agrees with the usual two-permutation
interleaver. Its deterministic R4 wiring does not. A one-hot discriminator is:

```text
inputs:  000000000001, 000000000000, 000000000000, 000000000000
legacy:  000000000000, 000000000004, 000000000000, 000000000000
IEEE:    000000000000, 000000000000, 000000000000, 000000000100
```

This conversion preserves the deterministic legacy result for comparison with
the source project, following the same compatibility policy used for the
Controller24 rate-code mismatch. A future standards-correct profile requires a
separate product decision; it must not silently replace this contract.

## Historical control defects not preserved

`Interleaver.bsv` computes an effective `this_rate`, but advances `inCnt` and
tags completed blocks from the old `cur_rate`. After reset this combines the
first rate-one SIGNAL word with the following data word and mislabels the
result. The source also places `?` in partial `mapR`; current BSC happens to emit
alternating `A` constants, which are not language semantics.

The project `-aggressive-conditions` flag improves readiness by allowing a
non-final chunk while the completed-block FIFO is full. It does not repair the
stale-rate or R4 numerical defects. ZLang states readiness and effective-rate
selection explicitly and never exposes undefined storage.

## Boundaries

This slice adds no syntax, IR node, backend dispatch, temporal property, or
formal observation family. Existing register/FIFO/ready-valid safety families
remain applicable. Padding, tail bits, puncturing, packet-end signaling,
standard-correct R4, Mapper, IFFT, and complete transmitter behavior are
outside the contract.
