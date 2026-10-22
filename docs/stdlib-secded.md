# Extended-Hamming SECDED

Import `std.coding.secded` for `secded_encode<K>(data)`,
`secded_decode<K>(received)`, `RvSecdedEncode<K>` and `RvSecdedDecode<K>`.
The stream modules admit payload widths K=1…64. R is the smallest integer
with `2^R >= K+R+1`; the codeword width is K+R+1. K=32 produces 39 bits,
and K=64 produces 72 bits.

Hamming positions start at 1. Positions 1,2,4,8,… contain parity bits;
remaining positions contain data in ascending order, least-significant data
bit first. The most-significant codeword bit is overall even parity.

Decode returns `SecdedDecoded<K,R>` with `data`, `syndrome`, `status` and
`data_valid`. Status is `SecdedStatus.clean`, `corrected` or
`detected_uncorrectable`. A single corrupt bit is corrected, including a
Hamming or overall parity bit. Every pair of corrupt bits is detected.
On an uncorrectable result, `data_valid=0`: `data` is not trustworthy.
The syndrome is a diagnostic position, not a safe arbitrary array index.
Out-of-layout syndromes in shortened codes never trigger indexed correction.
For three or more corrupt bits, miscorrection is possible; neither correction
nor detection is guaranteed.

Both stream modules have one buffered result stage, one-edge unstalled
latency and II=1. `input.valid && input.ready` accepts a word;
`output.valid && output.ready` retires it. A stalled result is held stable.
Retirement and replacement may occur on the same edge. Synchronous reset
clears validity and discards the pending word.

## Run the 39/32 example

After `make venv`, run from the repository root:

```sh
.venv/bin/zlang sim examples/secded.zhl --top Secded32Decode \
  --events examples/secded32.events.jsonl \
  --trace build/secded32.fst --json
```

The event file sends codeword zero, a single parity-bit error (`1`), then
two parity-bit errors (`3`), including a stalled output. Expected statuses
are clean, corrected and detected_uncorrectable. Inspect `output.valid`
before interpreting a result, and `output.payload.data_valid` before using
its data. `Secded32Encode` in the same source accepts bits<32> and produces
bits<39>; it can be connected to the decoder using ordinary ready/valid
composition.

Native U/X remains diagnostic: unresolved accepted data and stateful controls
are subject to the current strict-uninitialized observation contract. SECDED
does not turn an unknown simulator value into a reliable binary codeword.
There are no ECC RAM, scrubbing or Hsiao wrappers in this module.
