# DVB Reed–Solomon coding

`std.coding.rs_dvb` provides `RvRsDvb204188Encode`,
`rs_dvb_parity_step` and `rs_dvb_syndrome_step`.
`std.coding.rs_dvb_decode` provides `RvRsDvb204188Decode` with the same
byte/status record types. The separate import keeps encoder-only designs
independent of decoder arithmetic and control declarations.
`std.coding.gf256` supplies polynomial-basis `gf256_mul` and
`gf256_inverse`; inversion returns `Gf256Inverse {value,valid}`.
Zero returns value=0, valid=0 because it has no multiplicative inverse.

The profile is systematic RS(204,188), shortened from RS(255,239) by 51
leading zero symbols. It uses GF(256), primitive polynomial 0x11D, primitive
element 0x02, and generator roots α⁰…α¹⁵. This is the outer-code profile in
[ETSI EN 300 744 §4.3.2](https://www.etsi.org/deliver/etsi_en/300700_300799/300744/01.06.02_60/en_300744v010602p.pdf),
not RS(255,223) or CCSDS. No energy dispersal, MPEG parsing or interleaving
is performed.

Wire order is highest polynomial coefficient first. The first 188 output
bytes exactly reproduce the input, followed by 16 parity bytes. For input
bytes 0,1,…,187 the parity suffix is
`31 1d 78 d6 c8 60 f8 78 b7 18 9f 1a 54 96 1d 5f`.
The shortening zeros are never transmitted; appending zeros after the data
would describe a different computation.

## Encoder

Input and output payloads are `RsByteBeat {data:bits<8>, first:bit, last:bit}`.
Only ready/valid transfers advance a frame. Set first on byte zero and last
on byte 187. Both must be clear on intermediate input bytes. The entire
188-byte frame is stored and checked before any payload is emitted.

`status:rv<RsFrameStatus>` must retire before output begins. The record has
`status:RsStatus` and `correction_count:uint<4>`. Encoder status is clean or
malformed; correction_count is zero. Corrected/detected_uncorrectable are
decoder statuses, not encoder error-detection guarantees.
A malformed frame emits status only, never payload.

Missing first, an unexpected first, early last or a missing last at the
188-byte boundary is malformed. If the bad packet was unterminated, input
is discarded after status retirement until last or a new first marker.
The byte that triggered the error has already been consumed; it is not
replayed as another frame. A new first marker encountered while discarding
can start a fresh packet.

There is one frame in flight. Input is blocked during status wait and all
204 output bytes. Output first/last mark bytes zero/203, and stalled payload
is stable. Reset clears validity, framing, parity and the pending packet.
With continuous ready/valid and no reset, collection, status and drain take
188+1+204=393 edges, without overlap. Storage is an explicit 188-byte register
shift buffer; this implementation does not promise inferred BRAM.

## Decoder

Send exactly 204 received bytes with first on byte zero and last on byte203.
The decoder stores the full codeword before solving or producing output.
It corrects up to eight erroneous **symbols**, whether in data or parity;
multiple erroneous bits in one byte count as one symbol. Erasures are not
supported.

Status must transfer before any payload. A clean/corrected status is followed
by exactly188 data bytes, with first/last on bytes0/187. The correction count
is valid only for corrected status; other statuses return zero. A malformed
or detected_uncorrectable result has no payload. Reset discards collection,
solve and pending status/output. Output stalls preserve data and markers.

Malformed-marker recovery follows the encoder rules above, with the length
boundary at204 rather than188. No next frame is accepted during solve, status
wait or payload drain. Keep input valid/payload stable until ready accepts it.

The decoder uses Berlekamp–Massey, a complete255-position Chien search and
Forney correction. It rejects invalid degree/root counts, virtual shortening
positions and zero denominators, then recomputes all syndromes. Beyond eight
symbol errors, miscorrection to another valid codeword remains possible:
even zero final syndromes cannot guarantee the original message was recovered.

This implementation explicitly shares one variable GF multiplier across
BM updates and polynomial evaluation. It also has a combinational inverse
lookup and constant-coefficient syndrome arithmetic. It is not a one-cycle
decoder. With continuous input valid and status/output ready, and no reset:

| Successful frame | Edges from first input transfer through last output transfer |
| --- | ---: |
| Clean |394|
| Corrected, e=1…8 symbols |6341 + 36·e|

Stalls and input bubbles add edges; unsuccessful frames can stop earlier.
Storage is204 packet bytes plus polynomial/control registers, not inferred
BRAM. The module makes no routed Fmax guarantee.

## Run

```sh
.venv/bin/zlang sim examples/rs_dvb.zhl --top DvbRsEncoder \
  --events examples/rs_dvb.events.jsonl --trace build/rs_dvb.fst --json
```

The example sends bytes 0…187, accepts clean status, stalls the first output
byte, then drains the codeword. Consume only output transfers. This module
does not decode or assess channel errors; a clean *encoder* status means the
input framing was valid, not that a received packet passed a decoder.

The decoder example uses the same ramp codeword, with two corrupted symbols
(the first data byte and last parity byte). It returns corrected status with
count2 and the original bytes0…187:

```sh
.venv/bin/zlang sim examples/rs_dvb_decode.zhl --top DvbRsDecoder \
  --events examples/rs_dvb_decode.events.jsonl --trace build/rs_dvb_decode.fst --json
```

Observe status/output **transfers**, not only their valid values. The event
file includes the complete solve/drain time; stopping just after the final
received byte leaves the iterative decoder busy.
