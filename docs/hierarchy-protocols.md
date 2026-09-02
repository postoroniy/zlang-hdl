# Hierarchy, protocols, and composition

ZLang represents modules, endpoints, ownership, clock domains, connections, and
physical instances explicitly before backend lowering. Connections are never
inferred by guessing generated RTL names.

## Modules and instances

Modules may have compile-time type and value parameters:

```zlang
module Engine<type T, DEPTH=4> {
    in  value : T
    out copy  : T = value
}
```

Instantiate and bind scalar inputs explicitly or by same-name shorthand:

```zlang
inst engine : Engine<T=u8,DEPTH=4> { value }
result = engine.copy
```

Specialization identity is distinct from physical instance identity. A
single-domain parent may supply an unambiguous child clock/reset implicitly;
multi-domain hierarchy requires explicit compatible domains. Child protocol
endpoints use `connect`, not scalar binding syntax.

When every intermediate child conforms to one exact named interface with one
protocol input and one protocol output, an option-free path can be written as:

```zlang
input -> decode -> execute -> output
```

This lowers to the ordinary pairwise typed edges `input -> decode.rx`,
`decode.tx -> execute.rx`, and `execute.tx -> output`. Intermediate names are
physical instances, not guessed ports. Arrays, unnamed or ambiguous
signatures, edge options, adapters, buffering, and crossings remain explicit.

One-dimensional compile-time instance arrays are structurally unrolled:

```zlang
inst lane[N] : Lane<W>
generate(i in 0..N) {
    lane[i].x = values[i]
}
outputs = generate(i in 0..N) lane[i].y
```

A runtime selector may project one exact bit-packable wire output. This is a
read-only mux over the already elaborated children, never a dynamic physical
instance:

```zlang
selected = lane[select].output
```

Every `lane[i]` still exists, receives its compile-time bindings, and advances
its own state independently. The selector neither routes child inputs nor
gates a child clock, reset, rule, or storage action. The explicit equivalent is
`outputs = generate(i in 0..N) lane[i].output` followed by
`selected = outputs[select]`; both spellings retain the same physical child
identities.

The bounded supported form covers:

- combinational scalar children, including exact aggregate scalar outputs;
- single-domain scalar children with registers/rules;
- primitive ready/valid children, including scalar wire ports beside the
  endpoint and the existing one-FIFO profile;
- legacy storage-only children owning one FIFO, synchronous memory, or
  initialized ROM;
- the scheduled-FIFO profile in which FIFO actions and ordinary register writes
  share one existing `ResolvedTransition`; and
- an outer one-dimensional array whose element contains a bounded same-domain
  scalar or direct-ready-valid hierarchy. Inner compile-time scalar arrays are
  retained as distinct physical paths rather than flattened by generated name.

The first-level request/response profile is also bounded and explicit. Each
array child has exactly one `ordering in_order` endpoint plus scalar wire ports,
and each connection names compile-time indexed requester and responder elements:

```zlang
generate(i in 0..N) {
    requester[i].issue = issue[i]
    responder[i].accept = accept[i]
    connect requester[i].mem -> responder[i].mem
}
```

Every physical element retains its own outstanding ledger and reset epoch.
Out-of-order matching, an additional protocol on the same child, nested or
transitive request/response arrays, and unindexed endpoint references fail
closed. This profile is covered by compiler/backend tests and the executable
capability-registry witness
`tests/fixtures/hierarchy/request_response_instance_array.zhl`.

Legacy globally controlled storage still cannot be combined with user
registers/rules. Credit and other non-RV protocols, CSR or storage below a
nested array element, CDC arrays, runtime-selected inputs or protocol endpoints,
cross-module atomic scheduling, and incomplete bindings are rejected. See
[storage-owning instance arrays](storage-instance-arrays.md) for the exact state
and backend evidence.

Concise declarations normalize to the same hierarchy IR when unambiguous:

```zlang
frontend : AXI4LiteToRegBus<32,32>
axi : AXI4Lite<32,32>.slave @clk
axi -> frontend.axi
```

Explicit `inst` remains the escape hatch for namespace ambiguity.

### Typed external modules

A bounded scalar-wire component may declare backend-independent behavior with
an ordinary pure ZLang model:

```zlang
extern module VendorAdd : AddIfc { model add_model }
```

The concise source form is
`extern module VendorAdd : AddIfc { model add_model }`.

The applied interface and model produce one exact semantic signature. Physical
SystemVerilog is supplied separately through a hash-validated
`ExternalPhysicalMapping`; inline HDL never defines ZLang semantics. The first
release supports non-parameterized, clockless scalar inputs and one scalar
output in direct SystemVerilog. State, protocols, arrays, generic external
components and Clash physical mapping fail closed.

## Clock and reset boundary

Legacy `clock clk` plus synchronous active-high `reset rst` works through both
backends. A single-domain parent may supply that exact domain to a child only
when the mapping is unambiguous; this is domain inheritance, not implicit CDC.
One non-default falling-edge, asynchronous, or active-low physical contract is
typed and emitted by both direct SystemVerilog and Clash 1.11. Concise
`async reset` adds one root-owned two-edge release conditioner and passes the
conditioned reset through the closed child ABI; the raw asynchronous
compatibility form remains distinct. Instance arrays require one compatible
inherited domain unless they are purely combinational. Multi-domain async reset,
power-on reset, and implicit reset crossing remain fail-closed.

## Public top ABI

The selected top always exposes integration-friendly typed leaves. Struct
fields and structural tuple components, including those inside protocol
payloads, become individually named ports; tuple paths use `item0`, `item1`,
and so on. A vector leaf remains one native unpacked SystemVerilog array, for
example `vec<8,u8>` becomes `logic [7:0] samples [0:7]`; it is neither an
anonymous 64-bit public bus nor eight separately named ports. Nested
`vec<Struct>` values become one array per struct field, and `vec<Tuple>` values
become one array per tuple component.

Both direct SystemVerilog and Clash-generated RTL use this same public
`TopPhysicalABI`. Packed values are allowed only in the private core and child
component ABIs. The conversion follows the canonical packing order with
element zero in the most-significant region. There is no source annotation or
compiler option selecting another public ABI.

## Ready/valid

```zlang
in  rx : rv<u8>
out tx : rv<u8>

tx.payload = rx.payload
tx.valid = rx.valid
rx.ready = tx.ready
```

For an input endpoint, payload/valid enter the module and ready leaves it. Output
ownership is reversed. `.transfer` is a read-only protocol property meaning
`valid & ready`. A source stalled with `valid=1` and `ready=0` must hold payload
and valid stable.

Direct composition names source then sink:

```zlang
connect rx -> tx
connect buffered_rx -> buffered_tx { buffer 2 }
```

A ready/valid buffer is real FIFO state; it does not appear implicitly.

## Credit and request/response

`credit<T,N>` provides pulse-return flow control. `.transfer` is the physical
send after credit gating, `.credits` is the current bounded count, and `.return`
returns one credit. Counters begin at `N`.

Adapters are explicit:

```zlang
connect rx -> tx { adapter rv_to_credit }
connect crx -> rtx { buffer 2 adapter credit_to_rv }
```

Request/response endpoints declare ordering and outstanding capacity:

```zlang
interface mem : request_response<Request,Response> {
    max_outstanding 2
    ordering in_order
}
```

The requester owns request payload/valid and response ready; the responder owns
request ready and response payload/valid. Request and response channels expose
their own read-only `.transfer` events. Directional buffers are independent:

```zlang
connect requester.mem -> responder.mem {
    request_buffer 4
    response_buffer 2
}
```

Buffered but unaccepted requests are distinct from accepted outstanding
requests. The ledger increments on responder-side request transfer and
decrements on requester-side response transfer. Reset begins a new protocol
epoch. `ordering out_of_order` requires a matching scalar ID field; hierarchical
out-of-order composition remains outside the current bounded subset.

## Aggregate protocols and the standard library

The compiler-shipped `std` namespace maps to ordinary `.zhl` files below
`stdlib/`. The compiler knows generic aggregate schemas, roles, ownership,
hierarchy, and domains; it does not hard-code AXI/APB transactions.

Available source-authored profiles include RegBus, AXI4-Lite, APB, AXI-Stream,
Wishbone B4 Classic, bridges, and the CSR target:

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
}
```

Aggregate pass-through connects each member according to typed ownership,
including reverse ready:

```zlang
input -> output
```

Protocol member properties such as `.transfer`, FIFO observations, and
request/response channel events are part of typed semantics. They are not
arbitrary user-defined fields and cannot be assigned when read-only.

## Packets, virtual channels, and arbitration

`packet<T>` extends ready/valid with `.last`. Arbiters explicitly select source
order, `fixed_priority` or `round_robin`, and `beat` or `packet` grant lifetime.
`vc_credit<T,V,C>` maintains independent credit counts per virtual channel.

## CSR blocks

CSR source describes address layout, access policy, reset, and hardware binding
once. Supported policies are `rw`, `ro`, `wo`, `w1c`, `pulse`, and `reserved`.
Hardware status uses `<-`, commands use `->`, and sticky events make simultaneous
hardware/software priority explicit. RTL, JSON, and Markdown are derived from
the same typed model.

## Clock-domain crossings

Implicit CDC is an error. Existing explicit forms are:

| Crossing | Supported endpoint |
| --- | --- |
| `sync_level` | Slowly changing `bit` level |
| `pulse_toggle` | Rate-limited `bit` pulse |
| `handshake` | One scalar ready/valid payload |
| `async_fifo(N)` | Ready/valid stream through a power-of-two dual-clock FIFO |

```zlang
connect source -> destination { crossing async_fifo(4) }
```

An aggregate with exactly one forward ready/valid member may use the same
`async_fifo` crossing; its complete payload is atomic. Both domains must
participate in a coordinated reset episode.

No automatic protocol adaptation or implicit CDC is performed. Full AXI4
bursts/IDs and bus-specific CDC bridges remain deferred.
