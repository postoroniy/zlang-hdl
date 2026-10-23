# Framed K=7 Viterbi decoding

`std.coding.viterbi_k7` contains one rate-1/2 decoder engine:
`RvViterbiK7Soft<MAX_BITS=4096>`. `RvViterbiK7Hard` converts each received
bit to soft value0 or7 and uses that same engine. Capacity is1…4096 decoded
bits; a frame may finish before capacity is reached.

## Inputs and decision rule

Soft payload is `ViterbiSoftPair {q0:u3,q1:u3,first:bit,last:bit}`.
Zero means strongest zero, seven strongest one. For an expected bit0 the
cost is q; for bit1 it is7−q. Costs of the two received bits are added.
Hard payload is `ViterbiHardPair {pair:bits<2>,first:bit,last:bit}`;
pair[1] is G0 and pair[0] is G1.

The trellis matches `ieee_convolutional_encode24`: source bit0 first,
seven-bit window `{new_bit,history}`, masks0x5b/0x79 and next history
`{new_bit,history[5:1]}`. The initial state is zero. The final state is the
minimum-metric reachable state, not necessarily zero. Encoder finish clears
history after a word; it does not transmit tail bits.

Equal-cost paths select the smaller predecessor; equal final metrics select
the smaller state number. Reachability is separate from the metric, whose
width is `ceil_log2(14*MAX_BITS+1)`. There is no metric saturation or numerical
sentinel. The result is a minimum-metric path, not a promise to detect an
incorrectly decoded message.

## Frames, storage and timing

Set first on the first accepted pair and last on the final accepted pair.
`status:rv<ViterbiFrameStatus<MW,CW>>` reports malformed, decoded_bits and
path_metric; `MW=ceil_log2(14*MAX_BITS+1)`, `CW=index_width(MAX_BITS+1)`.
Malformed frames report zero length/metric and have no payload. Missing or
unexpected first, or reaching capacity without last, is malformed. An
unterminated bad frame is discarded until last or a new first marker after
status retirement. Reset abandons the current frame and all pending output.

Successful status must retire before `output:rv<ViterbiDecodedBit>` begins.
Its payload has data, first and last. The full survivor history is in RAM;
after end-of-frame the decoder traces back completely, stores decoded bits,
then emits them in the original order. No new frame overlaps traceback/drain.
Payload and markers remain stable during output stall.

Input accepts one pair per edge while receiving. Traceback takes two edges
per bit; drain takes two edges per bit without stalls. Ideal frame occupation
is5·N+1 edges including N input transfers, traceback, status and drain.
This is a framed implementation, not a continuous fixed-latency decoder.
Neither puncturing, tail-biting nor tail insertion/removal is performed.

## Existing 48-bit encoder words

`RvViterbiK7Word48` accepts `ViterbiK7Word48 {data:bits<48>,first:bit,last:bit}`.
It sends bits47:46 first, then45:44, and so on. Markers belong to the word
stream and must be supplied explicitly; they cannot be recovered from code
bits. MAX_BITS is24…4096. Whole-word frame lengths are multiples of24, so
the largest supported whole-word frame with default capacity is4080 bits.

## Run a hard-decision example

```sh
.venv/bin/zlang sim examples/viterbi.zhl --top ViterbiFrame \
  --events examples/viterbi.events.jsonl --trace build/viterbi.fst --json
```

The stimulus carries24 source bits from word0x123456, bit0 first. Advance
through input collection, traceback, status and the decoded bit stream in
the debugger. A live waveform is event-based, not a hardware timing estimate.
