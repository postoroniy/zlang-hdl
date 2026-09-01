# 802.11a convolutional encoder contract

Status: historical compatibility evidence. The reusable numerical findings are
retained by the canonical `src/conv_encoder.zl` implementation.

This note freezes the behavior extracted from the historical
`bluespec-80211atransmitter` encoder before implementing it as ordinary ZLang.
It is a conversion and language-validation contract, not a complete 802.11a
transmitter or standards-compliance claim.

## Numerical kernel

The concrete kernel consumes one 24-bit word and a six-bit committed history.
It processes source bits from bit 23 down to bit 0. With `h1` the most recently
accepted prior bit and `h6` the oldest, each source bit `x` emits:

```text
g0 = x ^ h2 ^ h3 ^ h5 ^ h6   // octal generator 133
g1 = x ^ h1 ^ h2 ^ h3 ^ h6   // octal generator 171
```

The pair `(g0,g1)` is appended MSB-first. Therefore output bits 47 and 46 are
the pair for input bit 23, while bits 1 and 0 are the pair for input bit 0.
After each bit, `x` becomes `h1` and the previous history shifts toward `h6`.

Every nonzero rate tag begins from zero history. A rate-zero word continues the
history committed by the previous accepted data word. The output preserves the
input tag. The block always emits 48 encoded bits; puncturing is outside it.

Frozen anchors are:

| history mode | input | encoded | next history |
|---|---:|---:|---:|
| zero | `0x000000` | `0x000000000000` | `0x00` |
| zero | `0x800000` | `0xdf2c00000000` | `0x00` |
| zero | `0x000001` | `0x000000000003` | `0x20` |
| zero | `0xffffff` | `0xe68fffffffff` | `0x3f` |
| zero | `0xd60040` | `0xeba189c037cb` | `0x00` |
| zero | `0xf30000` | `0xe667fe700000` | `0x00` |
| zero | `0x1fc762` | `0x039a31ae7f44` | `0x11` |
| continuation after `0x1fc762` | `0x1269ee` | `0x31b178257540` | `0x1d` |

Encoding the last input from zero history instead gives `0x037178257540`, so
that pair detects accidental history reset.

## Header/data merge

The public module has two independent sinks and one source:

```zlang
in  header_input  : rv<bits<24>>
in  word_input    : rv<RateWord24>
out encoded_output : rv<RateWord48>
```

Header and data arrivals are buffered independently. For a nonzero tagged word:

1. wait until one header is available;
2. emit the header as a `rate=1` encoded word without consuming the data word;
3. emit the retained data word on the following eligible transfer;
4. permit subsequent `rate=0` continuation words without another header;
5. require the next header when another nonzero tag reaches the data head.

The accepted raw rate tags are exactly `0`, `1`, `2`, and `4`. A sparse tag or
a rate-zero continuation before any accepted nonzero start does not progress.
The surrounding `Controller24` remains responsible for legal packet framing and
the continuation count because this transaction shape contains no `last` bit.

All three FIFOs have depth two. Output may be popped and replaced in the same
cycle according to existing scheduled FIFO semantics. While the output is
stalled its valid payload is stable. Reset synchronously clears all buffered
transactions, zeroes history, restores header-required state, and begins a new
protocol epoch.

## Historical BSC guard dependency

`ConvEncoder.bsv` names both FIFO heads before its conditional. A plain BSC
build consequently makes both `first()` method conditions unconditional rule
guards, and the single-header case can stall after header insertion. The
project Makefile uses `-aggressive-conditions`; with that option BSC predicates
the guards so the header is required only by the header branch and continuation
data drains normally.

ZLang expresses those conditions directly in typed guards. Correctness must not
depend on a compiler option that changes implicit method-condition scheduling.
The intended transferred sequence is the oracle; incidental BSC cycle timing
is not.

## Boundaries

This slice adds no new language syntax, temporal property, or backend dispatch.
It does not implement tail bits, padding, puncturing, final-byte masks, packet
identity, interleaving, mapping, IFFT, or a complete transmitter. Existing M35
register/FIFO/ready-valid/rule safety checks may be reused, but progress and
deadlock freedom are validated behaviorally rather than by adding liveness
infrastructure.
