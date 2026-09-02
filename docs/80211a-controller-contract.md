# 802.11a Controller24 contract freeze

Status: historical compatibility evidence. The executable compatibility module
has been retired; `src/controller.zhl` is the canonical IEEE framing unit.

## Scope and reference

This unnumbered real-design slice ports only the packet/rate controller from the
MIT-licensed Bluespec 802.11a transmitter at commit
`d654bfd4c2ffabc61437c131770beff58dc55b04`. It does not claim full IEEE 802.11a
compliance and does not start another numbered milestone.

The historical controller has one packet-info method, one 24-bit data method,
one header FIFO, one data FIFO, and packet-active state. Its valid header and
nonzero-length behavior is deterministic. The default rate translation is
Bluespec `?`, and internal length/rate registers use `mkRegU`; those undefined
cases are not an acceptable ZLang contract.

## Public transaction interface

```zlang
struct TxControl {
    rate   : u3
    length : u12
}

struct RateWord24 {
    rate : u3
    data : bits<24>
}

module Controller24 {
    clock clk
    reset rst
    in command  : rv<TxControl>
    in mac_data : rv<bits<24>>
    out header  : rv<bits<24>>
    out data    : rv<RateWord24>
}
```

Commands transfer only when the controller is idle, the header FIFO can accept
one item, `rate` is one of `1`, `2`, or `4`, and `length != 0`. An invalid
command remains unaccepted. Data transfers only while a packet is active and
the data FIFO has capacity.

## Header encoding and legacy-rate compatibility

The conversion deliberately preserves this deterministic legacy mapping:

| project rate tag | historical datapath intent | frozen legacy SIGNAL code |
|---:|---:|---:|
| 1 | 6 Mbit/s | `0b1101` |
| 2 | 12 Mbit/s | `0b1111` |
| 4 | 24 Mbit/s | `0b0101` |

This table is conversion compatibility, not a statement that the generated
SIGNAL field is standards-correct. The historical controller's codes for rate
tags 2 and 4 do not match their datapath comments/intended 12 and 24 Mbit/s
modes: the standards-intended codes would be `0b0101` and `0b1001`
respectively. This slice keeps the observable deterministic BSV behavior so
that the port has a stable oracle. A standards-facing correction must be an
explicit later design/profile decision, never a silent change to this
compatibility module.

The header representation is:

```text
[23:20] rate code
[19]    reserved zero
[18:7]  reverse12(length)
[6]     parity(rate_code || length)
[5:0]   zero
```

The parity bit is XOR reduction of the four rate-code bits and the original
twelve length bits. Bit reversal affects placement only, not parity. Frozen
anchors are `(rate=1,length=3) -> 0xd60040`, `(2,6) -> 0xf30000`, and
`(4,100) -> 0x513040`.

## Packet accounting

For accepted nonzero length `L`, the controller accepts exactly
`ceil(L / 3)` 24-bit data transfers. The first emitted `RateWord24` carries the
command rate. Every continuation word carries rate zero. The final word is not
byte-masked; the upstream MAC owns unused bits in a partial final word.

The header and data paths use independent depth-two FIFOs. A stalled header
does not stall active packet data while the data FIFO has capacity. A stalled
data consumer eventually backpressures `mac_data`. Header and data retirement
are independent. Completing the final word makes the controller idle for
command acceptance on the following cycle; it does not create a same-cycle
bypass between conflicting packet epochs.

## Reset and invalid inputs

Synchronous reset clears both FIFOs, clears `active`, sets remaining length and
current rate to zero, discards partial packet state, and begins a new protocol
epoch. The first valid post-reset command behaves like a fresh packet.

Rate zero, sparse invalid three-bit rate encodings, and length zero do not
transfer. This is a deliberate fail-closed boundary for source behavior that is
undefined or operationally useless in the historical implementation.

## Validation boundary

An independent integer/cycle oracle, not generated IR, defines header bits,
remaining-byte accounting, FIFO occupancy, ready/valid transfers, and reset.
Simulator, direct SystemVerilog, and Clash must agree with it. Existing M35
register/FIFO/ready-valid properties may be used without adding a new property
family. The later convolutional encoder remains separately review-gated because
its historical two-input merge can deadlock continuation data.

This slice does not add a final-word byte mask, standards-correct rate remapping,
the encoder, interleaver, mapper, IFFT, bus wrapper, or a new formal observation
family.
