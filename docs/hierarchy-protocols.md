<a id="reference-hierarchy-protocols"></a>
# Hierarchy, protocols, and explicit CDC


ZLang represents modules, endpoints, ownership, clock domains, connections, and
physical instances explicitly before backend lowering. Connections are never
inferred by guessing generated RTL names.

<a id="reference-hierarchy-protocols-modules-and-instances"></a>
## Modules and instances

Modules may have compile-time type and value parameters:

<!-- zlang-example: syntax-only; generic template requires specialization -->
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

A reusable component fingerprint covers its typed hardware and the deterministic
transitive closure of callables that hardware actually invokes. Functions that
are merely visible through a parent/import context do not participate. Thus the
same parameterless child reached through different parents is emitted once,
while different value/type specializations or a different reachable helper body
remain distinct and fail closed on an identity conflict.

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

Structural generation may be nested and may contain compile-time `if`/`else`.
Both are elaborated by the same bounded unroller; they do not create runtime
loops or dynamic topology:

```zlang
generate(i in 0..N) {
    generate(j in 0..N) {
        if i == j { lane[i].x = values[j] }
    }
}
```

The existing total expansion limit applies across the complete nested tree.
Exceeding it reports a bounded compile-time-generation diagnostic rather than
partially elaborating the design.

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
closed. This profile has an executable capability-registry witness and is
covered by compiler, simulator, and backend validation.

Legacy globally controlled storage still cannot be combined with user
registers/rules. Credit and other non-RV protocols, CSR or storage below a
nested array element, CDC arrays, runtime-selected inputs or protocol endpoints,
cross-module atomic scheduling, and incomplete bindings are rejected.

Concise declarations normalize to the same hierarchy IR when unambiguous:

```zlang
frontend : AXI4LiteToRegBus<32,32>
axi : AXI4Lite<32,32>.slave @clk
axi -> frontend.axi
```

Explicit `inst` remains the escape hatch for namespace ambiguity.

<a id="reference-hierarchy-protocols-typed-external-modules"></a>
### Typed external modules

A bounded scalar-wire component may declare portable behavior with
an ordinary pure ZLang model:

<!-- zlang-example: syntax-only; interface and model are defined separately -->
```zlang
extern module VendorAdd : AddIfc { model add_model }
```

The concise source form is
`extern module VendorAdd : AddIfc { model add_model }`.

The applied interface and model produce one exact semantic signature. Physical
SystemVerilog is supplied separately through a hash-validated
`ExternalPhysicalMapping`; inline HDL never defines ZLang semantics. The
supported form has non-parameterized, clockless scalar inputs and one scalar
output in Direct-SV. State, protocols, arrays, and generic external
components fail closed.

<a id="reference-hierarchy-protocols-clock-and-reset-boundary"></a>
## Clock and reset boundary

Legacy `clock clk` plus synchronous active-high `reset rst` works through the
production Direct-SV backend. A single-domain parent may supply that exact domain to a child only
when the mapping is unambiguous; this is domain inheritance, not implicit CDC.
Reusable stateful standard-bus cores use this same rule; source-authored pin
adapters and concrete application tops remain the external domain owners.
One non-default falling-edge, asynchronous, or active-low physical contract is
typed and emitted by Direct-SV. Concise
`async reset` adds one root-owned two-edge release conditioner and passes the
conditioned reset through the closed child ABI; the raw asynchronous
compatibility form remains distinct. Instance arrays require one compatible
inherited domain unless they are purely combinational. Multiple independently
conditioned asynchronous domains are supported in Direct-SV. Every domain of a
stateful child must match exactly one parent physical clock/reset contract;
no implicit reset crossing is inserted. Multi-domain asynchronous formal
execution, power-on reset, and implicit reset crossing remain fail-closed.

<a id="reference-hierarchy-protocols-public-top-abi"></a>
## Public top ABI

The selected top always exposes integration-friendly typed leaves. Struct
fields and structural tuple components, including those inside protocol
payloads, become individually named ports; tuple paths use `item0`, `item1`,
and so on. A vector leaf remains one multidimensional packed SystemVerilog
array; for example `vec<8,u8>` becomes `logic [7:0][7:0] samples`. It is
neither an anonymous 64-bit public bus nor eight separately named ports. Nested
`vec<Struct>` values become one array per struct field, and `vec<Tuple>` values
become one array per tuple component.

Direct-SV RTL uses this public `TopPhysicalABI`. The conversion
follows the canonical packing order with element zero in the least-significant
region: for a descending packed vector dimension, source element zero maps to
physical index `0`. There is no source annotation, compiler option, or
backend-specific preprocessor branch selecting another public ABI.

<a id="reference-hierarchy-protocols-ready-valid"></a>
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

<a id="reference-hierarchy-protocols-credit-and-request-response"></a>
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

<a id="reference-hierarchy-protocols-aggregate-protocols-and-the-standard-library"></a>
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

<a id="reference-hierarchy-protocols-packets-virtual-channels-and-arbitration"></a>
## Packets, virtual channels, and arbitration

`packet<T>` extends ready/valid with `.last`. Arbiters explicitly select source
order, `fixed_priority` or `round_robin`, and `beat` or `packet` grant lifetime.
`vc_credit<T,V,C>` maintains independent credit counts per virtual channel.

<a id="reference-hierarchy-protocols-csr-blocks"></a>
## CSR blocks

CSR source describes address layout, access policy, reset, and hardware binding
once. Supported policies are `rw`, `ro`, `wo`, `w1c`, `pulse`, and `reserved`.
Hardware status uses `<-`, commands use `->`, and sticky events make simultaneous
hardware/software priority explicit. RTL, JSON, and Markdown are derived from
the same typed model.

Status and sticky sources may select exact leaves of a typed aggregate input;
the compiler retains and type-checks the member expression rather than deriving
a flattened name:

```zlang
struct Perf { cycles : u32 }
struct EngineStatus { busy : bit perf : Perf }

module AggregateStatusBank {
    clock clk reset rst
    in status : EngineStatus
    csr registers @0 {
        STATUS @0 { busy bit @0 ro <- status.busy }
        CYCLES @4 { value u32 @31:0 ro <- status.perf.cycles }
    }
}
```

Aggregate members used by one CSR bank must belong to that bank's clock
domain. Command bindings remain scalar output ports.

Omitted bits are implicitly reserved. A storage-independent access event may
observe the same bits as their single owning field:

```zlang
module CsrAccessEventExample {
    clock clk reset rst
    in fault_status : bits<32>
    out fault_clear, registered_clear : bits<2>
    csr registers @0 {
        FAULT @0x470 {
            value bits<32> @31:0 ro <- fault_status
            clear bits<2> @1:0 on_write -> fault_clear
            clear_later bits<2> @1:0 on_write post_accept -> registered_clear
        }
    }
}
```

The event is address-qualified by the compiler and owns no field state. Its
default `active_transfer` phase is combinational during the qualified access.
An explicit `post_accept` phase captures the selected event value at the
accepting edge, publishes it for the following cycle, and then clears it when
there is no new matching access. This phase does not change CSR readback or the
canonical rule that `rdata` is meaningful only for reads. Stored,
external-status, and event observations are available through typed child
namespaces `state`, `status`, and `events`. JSON and Markdown maps record the
selected event phase.

Repeated layouts flatten into one bank and one decoder:

```zlang
module CsrGroupExample {
    clock clk reset rst
    csr group Window { CONTROL @0 { enable bit @0 rw = 0 } }
    csr control @0 { windows : Window[13] @0x210 stride 4 }
}
```

Group counts are bounded to 1..64 and a bank to 256 expanded registers. A
logical 64-bit value may retain two independently writable 32-bit software
registers:

```zlang
module CsrSplitExample {
    clock clk reset rst
    csr registers @0 {
        INPUT_BASE @0x18 split<32> value u64 rw = 0 order low_first
    }
}
```

The same `split<32>` declaration may be part of a reusable group. Every group
instance receives one logical typed projection and two independently writable
32-bit words:

```zlang
module GroupedSplitExample {
    clock clk reset rst
    csr group Window {
        CONTROL @0 { enable bit @0 rw = 0 }
        BASE @4 split<32> value u64 rw = 0 order low_first
    }
    csr registers @0 { windows : Window[2] @0 stride 12 }
}
```

This does not imply byte strobes or an atomic two-word write. Typed command
capture and registered RegBus timing are ordinary source modules:
`StorageSnapshot<T,reset_value>` in `std.storage.core` and
`RegisteredCSRTarget<AW,DW>` in `std.bus.reg`; the compiler has no special case
for either module name.

In a multi-clock module the bank declares its owner after its base address:

```zlang
csr control @0x4000_0000 @apb_clk { /* registers */ }
```

All blocks sharing the canonical access ABI must use that one domain. A
runtime CSR value consumed by another domain still requires explicit CDC.

<a id="reference-hierarchy-protocols-clock-domain-crossings"></a>
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

The crossing is a semantic boundary. Its output has destination-domain
provenance and may feed destination-domain combinational logic, rules, FSMs, or
state normally. The compiler never rewrites, balances, or resource-packs logic
through the boundary, and never chooses a crossing automatically.

```zlang
connect enable_a -> enable_b { crossing sync_level }
rule Observe @clk_b when enable_b { seen <- 1 }
```

An aggregate with exactly one forward ready/valid member may use the same
`async_fifo` crossing; its complete payload is atomic. Both domains must
participate in a coordinated reset episode.

The physical Direct-SV FIFO uses one dedicated 1W1R `async_mem` with a
one-cycle registered read, two-stage Gray-pointer synchronizers, and one
prefetched output beat. A stalled beat holds both payload and `valid`; the
read/consumed pointer advances only on transfer, so that beat still occupies
capacity. Continuous accepted beats can transfer at II=1 without an extra
output bubble. The synchronizers model digital CDC behavior, not analog
metastability or an MTBF guarantee.

The ordinary `std.storage.core.StorageAsyncFifo<T,D>` wrapper exposes this same
crossing as a reusable module with explicit writer and reader clock/reset
ports. It lowers through the existing CDC IR; its module name is not special
to the compiler. A normal `fifo<T,N>` never changes into an asynchronous FIFO
because its endpoints happen to use different domains.

No automatic protocol adaptation or implicit CDC is performed. The source-owned
`std.bus.axi4` modules provide bounded IDs, bursts and optional USER sidebands;
complete external AXI4 endpoint compliance is not qualified. See the
[standard bus contracts](standard-bus-library.md) for supported profiles and
evidence boundaries. Bus-specific CDC bridges remain deferred.
