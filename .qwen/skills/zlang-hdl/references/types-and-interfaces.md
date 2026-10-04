# Types and interfaces

Use this reference when choosing a ZLang type, changing representation, or
connecting flow-control and standard-bus endpoints. Confirm a construct against
the checked-out compiler, `docs/language-reference.md`, and the cited runnable
example before emitting source.

## Choose an exact value type

| Family | Spellings | Use |
| --- | --- | --- |
| Control | `bit` | One-bit control; distinct from integers and raw bits. |
| Unsigned integer | `u8`, `u16`, `u32`, `uint<N>` | Arithmetic and ordering on an exact unsigned width. |
| Signed integer | `s8`, `s16`, `s32`, `sint<N>` | Two's-complement arithmetic on an exact signed width. |
| Raw representation | `bits<N>` | Encodings, masks, and fields without integer arithmetic. |
| Fixed point | `fixed<W,F>`, `ufixed<W,F>` | Signed/unsigned fixed point with wrapping target narrowing. |
| Saturating fixed point | `fixed_sat<W,F>`, `ufixed_sat<W,F>` | Fixed point whose target narrowing saturates. |
| Concise fixed point | `SF8.8`, `SF_Sat8.8`, `UF8.8`, `UF_Sat8.8` | Equivalent concise signed/unsigned profiles. |
| Character/string | `char`, `string<N>` | Canonical `u8` and `vec<N,u8>`; fixed hardware size, no hidden terminator. |

Widths are positive compile-time integers. Fixed point requires `0 <= F < W`.
These families do not implicitly convert between signed, unsigned, raw-bit, or
fixed-point representations. A source alias such as `type Address = uint<32>`
normalizes to its canonical type and does not create a nominal wrapper.

## Choose an aggregate or storage type

- `vec<N,T>` is a fixed homogeneous vector. Element zero occupies the
  least-significant packed component.
- `struct` is a nominal named product; first-declared fields occupy the most
  significant packed region. Construct with `Type { field=value ... }`, update
  with `value { field=new_value }`, and select with `value.field`.
- `enum` is nominal control state. Use qualified members and `enum_encode`,
  `enum_valid<T>`, and total `enum_decode<T>` at raw boundaries.
- `union` is a nominal tagged union. Construct a named variant and consume it
  with an exhaustive `match`; do not inspect the tag as an integer. Union values
  may cross module inputs and outputs.
- `(T,U)` is a structural tuple. Project with literal indices or destructure
  exhaustively.
- `reg`, `fifo name : fifo<T,D>`, `memory name : mem<T,D>`,
  `memory name : async_mem<T,D>`, and `rom name : rom<T,N>` are
  state/storage declarations with clock, reset, latency, port, and collision
  semantics. They are not freely interchangeable value types.

Public struct and tuple ports become recursively named leaves. A vector leaf
stays a multidimensional packed SystemVerilog array, for example `vec<8,u8>` is
`logic [7:0][7:0]`. A public tagged union remains one named packed structure
with exact `tag` and `payload` fields; an external input producer must drive a
declared tag code. Never guess a physical name; consume the artifact's logical
bindings and source map.

## Manipulate representation explicitly

| Operation | Contract |
| --- | --- |
| `extend<N>(x)` | Explicit widening to exactly `N` bits, preserving signedness. |
| `truncate<N>(x)` | Explicit low-bit narrowing to exactly `N` bits. |
| `quantize<T>(x)` | Explicit fixed-point scale/width conversion using `T`'s rounding/overflow policy. |
| `bitcast<T>(x)` | Equal-packed-width representation change; no resize. |
| `pack(x)` / `unpack<T>(x)` | Low-level compatibility boundary; prefer `bitcast` for equal-width typed conversion. |
| `concat(a,b,...)` | Concatenation with the first operand at the most-significant side. |
| `reshape<T>(x)` | Shape change with an exact equal total packed width, for example `reshape<vec<2,vec<4,u8>>>(flat)`. |
| `repeat<N>(x)`, `zeros<N>()`, `ones<N>()` | Compile-time-sized construction. |
| `x[i]`, `x[MSB:LSB]` | Element/bit selection and inclusive static packed slice. |
| `values[first..past_last]` | Half-open vector range. |

Arithmetic result widths are compiler-defined. Do not add a destination type
and assume it resizes operands. Keep narrowing and fixed-point quantization at
the specified semantic boundary, especially around products and reductions.

## Choose an interface

| Interface | Flow-control contract | Start from |
| --- | --- | --- |
| `wire<T>` | Typed unidirectional value without handshake. | `docs/language-reference.md` |
| `rv<T>` | `payload`, `valid`, reverse `ready`; transfer is `valid & ready`. | `examples/rv_passthrough.zhl` |
| `credit<T,N>` | `payload`, `send`, returned-credit pulse, bounded count initialized to `N`. | `examples/credit_source.zhl` |
| `packet<T>` | Ready/valid packet beat plus `last`. | `examples/packet_fixed_arbiter.zhl` |
| `vc_credit<T,V,C>` | Credit flow with independent per-VC counts and explicit VC selection. | `examples/vc_credit_source.zhl` |
| `request_response<Req,Rsp>` | Two directional ready/valid channels with bounded accepted outstanding requests. | `examples/request_client.zhl` |
| named `protocol` | Compiler-checked roles, channels, scalar members, ownership, and domains. | `examples/hierarchical_protocol.zhl` |

Interface observations such as `.transfer` and `.credits` are read-only
language-defined status values. Drive only members owned by the endpoint's role.
Connect source to sink explicitly; no adapter, buffer, or CDC appears by
implication.

## Ready/valid

An input endpoint receives payload/valid and drives ready. An output endpoint
drives payload/valid and receives ready:

```zlang
module RvPassthrough {
  in rx: rv<u8>
  out tx: rv<u8>

  tx.payload = rx.payload
  tx.valid = rx.valid
  rx.ready = tx.ready
}
```

When `valid=1` and `ready=0`, the source must keep valid and payload stable.
`.transfer` is the actual accepted beat. Direct composition names source then
sink; buffering adds real FIFO state:

```zlang
connect rx -> tx
connect buffered_rx -> buffered_tx { buffer 2 }
```

## Credit flow

`credit<T,N>` starts with `N` credits. A requested send becomes `.transfer`
only when credit is available; `.return` returns one credit and `.credits`
observes the bounded count.

```zlang
module CreditSource {
  clock clk
  reset rst
  in payload_data: u8
  in request: bit
  out tx: credit<u8, 2>

  tx.payload = payload_data
  tx.send = request
}
```

Ready/valid conversion is explicit and stateful:

```zlang
connect rx -> tx { adapter rv_to_credit }
connect crx -> rtx { buffer 2 adapter credit_to_rv }
```

Use `examples/rv_to_credit.zhl` and `examples/credit_to_rv.zhl`; do not hand-roll
a counter unless the hardware contract is intentionally different.

## Request/response and named protocols

```zlang
interface mem : request_response<Request,Response> {
    max_outstanding 2
    ordering in_order
}
```

The requester owns request payload/valid and response ready. The responder owns
request ready and response payload/valid. Accepted requests, not merely buffered
requests, occupy the outstanding ledger. Directional buffering is independent:

```zlang
connect requester.mem -> responder.mem {
    request_buffer 4
    response_buffer 2
}
```

`ordering out_of_order` requires a matching scalar ID. A named `protocol`
should be used when the contract has several channels or reverse scalar members;
declare roles and ownership instead of modeling it as a freely writable struct.

## Choose a standard bus profile

All standard buses are ordinary source-owned ZLang protocols/modules. The
compiler has no AXI/APB/AHB transaction special case.

| Import | Supported profile and intended use | Runnable witness |
| --- | --- | --- |
| `std.bus.reg` | In-order request/response CSR boundary and CSR target. | `examples/axi_csr_top.zhl` |
| `std.bus.axi_lite` | AXI4-Lite, independent AW/W buffering, `AXI4LiteToRegBus`. | `examples/axi_csr_top.zhl` |
| `std.bus.apb` | APB setup/access sequencing and `APBToRegBus`. | `examples/apb_csr_top.zhl` |
| `std.bus.ahb_lite` | Bounded standards-correct AHB-Lite and `AHBLiteToRegBus`. | `examples/ahb_csr_top.zhl` |
| `std.bus.axi_burst` | No-ID, one-outstanding, full-width incrementing 1-256-beat subset and helpers. | `examples/axi_single_beat.zhl`, `examples/ztpu_axi_burst.zhl` |
| `std.bus.axi4` | Five AXI4 channels, IDs, bursts, bounded managers, optional USER profile. | `stdlib/bus/axi4.zhl` |
| `std.bus.axi4_subordinate` | Bounded read/write subordinate adapters. | `stdlib/bus/axi4_subordinate.zhl` |
| `std.bus.axi4_exclusive` | Explicit reservation and same-edge exclusive commit contract. | `stdlib/bus/axi4_exclusive.zhl` |
| `std.bus.axi4_pins` | Flat-pin projection of the source-owned five-channel interface. | `stdlib/bus/axi4_pins.zhl` |
| `std.bus.axi_stream` | Ready/valid stream with data/keep/strb/last and pipe. | `examples/streaming_packet_engine.zhl` |
| `std.bus.wishbone` | Wishbone B4 Classic single-beat bridge to RegBus. | `examples/wishbone_csr_top.zhl` |

For CSR integration, instantiate the supplied bridge and connect the aggregate
endpoint rather than wiring flattened members:

```zlang
import std.bus.reg
import std.bus.axi_lite

module AxiCsrTop {
    clock clk
    reset rst
    interface axi : AXI4Lite<32,32>.slave @clk
    inst frontend : AXI4LiteToRegBus<32,32>
    inst csr : RegBusCSRTarget<32,32>
    connect axi -> frontend.axi
    connect frontend.regbus -> csr.regbus
    out done : bit
    done = csr.done
}
```

The APB shape substitutes `APB<32,32>.slave` and `APBToRegBus`. AHB-Lite uses
`AHBLite<32,32>.slave`, `AHBLiteToRegBus`, and the exact active-low asynchronous
physical reset contract shown in `examples/ahb_csr_top.zhl`; connected stateful
RegBus logic must share that domain/reset.

Do not confuse `std.bus.axi_burst` with full AXI4. The full `std.bus.axi4`
profile supplies five independent channels, IDs, bounded outstanding slots,
bursts, and optional USER payloads, but does not claim arbitrary drop-in
endpoint compliance. Start from its source-owned managers/subordinates and
preserve every channel's independent handshake.

## Fail closed

- Do not infer a cast, resize, quantization, protocol adapter, buffer, or CDC.
- Do not assign `.transfer`, `.credits`, or other read-only observations.
- Do not treat protocol payloads as flattened backend names.
- Do not offer an unimported bus or synthesize an automatic bridge.
- Do not claim general AXI4 compliance from the bounded source profile.
- If a cited example no longer compiles, treat the skill as stale and follow
  the compiler plus current language reference.
