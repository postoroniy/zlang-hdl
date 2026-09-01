# SimpleDMA M40 validation

> **Document status:** this is a chronological M40 composition log. Early
> “known boundary” and “next slice” paragraphs intentionally record what was
> missing at that checkpoint; many were closed by later unnumbered hierarchy,
> protocol, instance-array, and backend slices. Use the
> [current status snapshot](current-language-status.md) and
> [syntax matrix](syntax-support-matrix.md) for present support. The retained
> SimpleDMA design itself passes semantic/canonical processing, both RTL
> backends, Verilator, and its applicable existing verification checks.

The generic top-level aggregate ABI is now available for source-defined
protocol boundaries. This does not change SimpleDMA semantics; future
memory-facing integration can use the same role-derived leaf directions and
manifest bindings without a bus-specific backend.

This document records the first real-design validation of the completed M40
subset. The target began with `AddressGen`, `RequestBuilder`, and the
hierarchical `SimpleDMA` parent. It now includes a mixed scalar/protocol
`TransferEngine` and a direct depth-two request FIFO connection in
`examples/simple_dma_m40.zl`.

## Design slice

- `TransferEngine<AW>` owns the request address counter and emits a typed
  ready/valid `MemRequest` while exposing scalar control/status ports.
- `SimpleDMA<AW>` wires the engine directly to its memory-facing ready/valid
  output with a depth-two FIFO, without flattening engine state.

## Validation log

### 1. Source parsing and semantic elaboration — passed

The source parses as one compilation unit, resolves `AW`, validates the struct
constructor, registers, rule, and instance specialization identities. The
initial parent IR retained two `Instance` records and the final hierarchical
field expression; the concise design retains one elaborated instance identity
and one explicit buffered hierarchical protocol connection.

### 2. Clash identifier sanitization — fixed

The design used a legal ZLang port named `data`, which is a Haskell keyword.
Clash emission initially generated invalid Haskell. The backend now applies a
small reserved-word escape (`data_zlang`) while retaining the original public
port name in annotations and semantic bindings. This was a backend-only fix;
the language did not need a naming restriction.

### 3. Minimal hierarchy elaboration — fixed

The semantic IR now contains explicit `InstancePortBinding` records and typed
`InstanceOutputRef` expressions. The parent retains child modules and instance
identity separately from specialization identity. Clash emits pure child core
functions and applies them over parent signals in the parent `where` block; it
does not perform textual placeholder replacement or optimization flattening.

The smallest failing form before this fix was:

```zlang
module P { out y:u8 inst c:Leaf y = c.y }
```

This was an elaboration/backend gap, not a language parsing gap. The minimal
scalar child-to-parent path is now implemented.

`AddressGen<8>` and `RequestBuilder<8>` pass real Clash Verilog generation and
Verilator lint independently. `SimpleDMA<8>` now also passes real Clash Verilog
generation and Verilator lint. A Verilator executable testbench drives reset and
one request and checks the packed struct fields; it exits successfully.

The repository's existing M35/M36 solver-backed checks remain green (4 tests in
the formal integration smoke suite). They validate the shared formal foundation,
but do not claim a parent DMA proof while the parent RTL artifact is unavailable.

### 4. Multiple outputs — known backend boundary

The concise DMA exposes one ready/valid aggregate output. Independent scalar
status outputs remain subject to the existing single-output boundary.

### 5. Sequential child elaboration — fixed

`ElaboratedInstance` now records the concrete child, inherited clock, and reset;
semantic elaboration rejects sequential children without a parent domain or
with mismatched clock/reset names. Child register/rule state is emitted as a
hidden-clock/reset Clash signal function, so state remains inside the child
hierarchy rather than being flattened into the parent.

The DMA transfer counter now lives in `TransferEngine<AW>`. Its omitted
clock/reset declarations inherit `SimpleDMA`'s `clk/rst`; the resulting child
IR and `ElaboratedInstance` retain those concrete names. Existing explicit
clock/reset declarations remain accepted. Real Clash Verilog generation and
Verilator lint pass for the concise source.

The existing real-solver M35/M36 integration suite was rerun while validating
this change. Its failure-first mutation classification is green, including the
wrong-arithmetic and wrong-latency regressions; the hierarchy code does not
participate in those fixtures.

### 6. Hierarchical ready/valid endpoints — Clash composition slice

Hierarchical `connect p.tx -> f.rx` paths now use explicit `ProtocolEndpoint`
and `HierarchicalConnection` IR records. They retain owner, direction,
ready/valid protocol, payload type, capacity, clock domain, and source identity.
A minimal producer → FIFO → consumer graph elaborates successfully and validates
endpoint compatibility before lowering.

The Clash backend now emits reusable child signal functions using the shared
`ZLangReadyValidForward`/`ZLangReadyValidBackward` records. The parent preserves
child state and wires the physical handshake explicitly:

```text
source.valid   -> sink.valid
source.payload -> sink.payload
sink.ready     -> source.ready
```

The FIFO child reuses the existing depth-two simultaneous push/pop, full/empty,
reset, and stall implementation as a child circuit body; it is not flattened or
rewritten by RTL-name substitution. Child specialization and instance names
remain separate in semantic IR, and the Clash manifest publishes endpoint,
payload, valid, ready, and FIFO-state bindings with semantic identities.

`ProtocolTop` now emits the producer, FIFO, and consumer functions plus explicit
hierarchical equations. The semantic, Clash-source, and manifest tests pass.
Using the repository Clash 1.11.0 executable, real Verilog generation,
Verilator lint, and a clock/reset simulation all pass; the simulation observes
the producer value `7` after reset. M35 formal mutation classification remains
failure-first and its focused solver suite is unchanged.

The next concrete blocker is applying the existing ready/valid and FIFO M35
properties directly to the composed parent (the current simulation covers the
traffic path, reset, and preserved FIFO state but is not a generated formal
harness). No protocol adapter, CDC, AXI, package, blackbox, or cross-module
optimization was introduced.

### 7. Mixed scalar/state/protocol children — closed Clash component ABI

The smallest reproducer has one `start:bit` scalar input, one `busy:bit` scalar
output, one `req:rv<Req>` output, and one counter register. The previously
generated separate-argument child failed Clash 1.11 normalization with an
unbound parent scalar (`parent_start`). Reduction exposed two related ABI risks:
aggregate expressions had not always contributed all of their signal leaves,
and a nested signal function could consequently retain a reference owned by the
parent scope.

```zlang
module Engine {
    clock clk reset rst
    in start:bit
    out busy:bit
    out req:rv<u8>
    reg count:u8 = 0
    rule step when req.transfer { count <- truncate<8>(count + 1) }
    busy = start
    req.payload = count
    req.valid = start
}
```

The aggregate dependency traversal was completed, then both ABIs were emitted
from the same reduced semantic IR and compiled with Clash 1.11.0:

| ABI | Clash result | Closure property |
| --- | --- | --- |
| Separate `Signal` arguments and tuple result | passes after aggregate-leaf repair | relies on every expression substitution being complete |
| Bundled `Signal dom ChildInput -> Signal dom ChildOutput` | passes | transition has no `Signal` values and cannot capture a parent signal |

The bundled form is now the ABI for a stateful mixed child. `ChildInput`
contains every scalar input, incoming forward endpoint, and outgoing backward
ready value. `ChildOutput` contains every incoming backward-ready value,
outgoing forward payload/valid record, and scalar output. State is implemented
by a pure function:

```haskell
childTransition :: State -> ChildInput -> (State, ChildOutput)
child :: HiddenClockResetEnable dom
      => Signal dom ChildInput -> Signal dom ChildOutput
child = mealy childTransition initialState
```

The parent constructs and projects these records explicitly. A `NOINLINE`
boundary retains the stateful child as a distinct generated Verilog component.
Tests inspect the transition text and reject any `parent_` reference in it.
Backward ready is an ordinary `ChildInput` field, so the rule advancing the DMA
counter uses the same `valid && ready` transfer event as the semantic IR.

`SimpleDMA<8>` now instantiates `TransferEngine<8>` and connects
`engine.req -> mem { buffer 2 }` directly. The direct form lowers to the same
`HierarchicalConnection(buffer_depth=2)` and FIFO equations as an explicit
wrapper. Real Clash emits the top and a separate `protocol_transferEngine`
Verilog component; Verilator lint passes. The cycle simulation fills the FIFO
while `mem_ready` is low, verifies that address 10 remains stable at the head,
releases backpressure, exercises simultaneous pop/push, observes addresses 11
and 12 in order, then drains to invalid. This checks reset, no request
duplication/loss, and transfer-gated count progression.

### 8. Concise composition syntax — passed

The checked-in example is the concise form:

```zlang
module TransferEngine<AW=8> {
    in start:bit
    in base:uint<AW>
    in data:u8
    interface mem:request_response<MemRequest,MemResponse>{max_outstanding 1 ordering in_order}
    reg count:uint<AW> = 0
    rule advance when mem.response.transfer { count <- truncate<AW>(count + 1) }
    mem.request.valid = start
    mem.request.payload = MemRequest { addr=truncate<8>(base + count) data=data write=1 }
    mem.response.ready = start
}

module SimpleDMA<AW=8> {
    clock clk
    reset rst
    in base:uint<AW> in data:u8 in start:bit in accept:bit
    out busy:bit
    inst engine:TransferEngine<AW> { start base data }
    inst memory:MemoryModel { accept data }
    connect engine.mem -> memory.mem { request_buffer 2 }
}
```

It replaces `TransferEngine<AW=AW>`, three separate `engine.port = value`
statements, and the `RequestFifo` wrapper. The verbose forms remain valid. The
concise and verbose scalar/instance forms produce equal semantic and canonical
IR, including specialization, instance, source-origin, clock/reset, and
connection-buffer metadata. `request_buffer 2` is directional FIFO state, not
formatting.
The checked-in example is 44 lines versus 55 lines in the pre-refinement
version (20% fewer source lines) while retaining the same elaborated behavior.

## Historical M40 checkpoint status

Parsing, type checking, specialization, struct construction, register/rule
semantics, source-origin retention, inherited domains, concise instance
bindings, direct buffered connections, preserved scalar/sequential hierarchy,
and the SimpleDMA simulation pass. The minimal ready/valid hierarchy and mixed
stateful hierarchy emit real Clash/Verilog and publish stable bindings; Clash
1.11 and Verilator execution pass. The full repository baseline is now green.
Nested protocol forms beyond the one-endpoint-per-child slice, adapters,
instance arrays, and cross-module optimization remain concrete follow-up gaps.
No packages, blackboxes, AXI, CDC, descriptors, or new milestone were
introduced.

### 9. Regression baseline restored

The pipeline golden mismatch was an emitter-only ordering regression: the
`pipeline(auto)` annotation had moved after `createDomain` even though semantic
and selected pipeline results were unchanged. The annotation is again emitted
in its established position. The runtime-index negative case was stale under
M40: `u1` indexes `vec<2,T>` exactly, so the test now uses `u2` against
`vec<3,T>`, whose encoded value 3 is out of range. The complete repository
regression is green after these corrections (`539` tests passed).

### 10. Hierarchical request/response composition

`hierarchical_request_response_m40.zl` uses the existing `request_response`
declaration with `max_outstanding 1` and `ordering in_order`. Requester and
responder ownership is inferred from existing channel assignments; no role or
transport syntax was added. A logical connection lowers to two explicit
ready/valid channel edges: request travels requester to responder, while
response travels in the opposite physical direction. Payload, valid, ready,
clock/reset, channel identity, and source ownership remain in
`ProtocolEndpoint`/`HierarchicalConnection` records. A request buffer applies to
the request channel only and reuses the existing FIFO state/update equations.

Both child components use the closed bundled ABI: scalar inputs and peer
handshake records are `ChildInput` fields, while owned request/response records
and scalar outputs are `ChildOutput` fields. Transaction accounting is kept in
the parent connection ledger (the child transition is intentionally not a
second counter); no parent `Signal` is captured. Real Clash 1.11
Verilog generation emits separate requester/responder components and Verilator
lint passes. The executable simulation exercises reset, request stall and
acceptance, response hold under backpressure, and response acceptance.
M33/M35 generic ready/valid and FIFO checks remain applicable; a dedicated
request/response automatic-property family is not yet present, so no stronger
formal claim is made. The shared-buffer-depth bug is fixed: `request_buffer N`
and `response_buffer N` are independent channel metadata, and generic `buffer N`
is rejected as ambiguous on request/response connections. Manifests publish
separate request/response FIFO state identities.

`examples/simple_dma_m40.zl` now makes `TransferEngine` the requester of a
`request_response<MemRequest,MemResponse>` endpoint and connects it directly to
the stateful `MemoryModel` responder. No ready/valid adapter is inserted. Real
Clash 1.11 generation and Verilator lint/build/simulation pass for SimpleDMA<8>,
including request-only and both-direction buffering. Reset clears generated
FIFO and child transition state; the child advances only on response transfer,
while request FIFO admission remains distinct. The reviewed accounting/reset
architecture is documented in this report;
max-outstanding values above one, out-of-order matching, adapters, and bus
protocols remain excluded.

The subsequent composition review extended hierarchy to bounded in-order
`max_outstanding=N` without IDs, using one small cross-channel
transaction descriptor while retaining the existing physical channel IR. No
implementation was started during that review.

### 11. Multi-outstanding request/response slice

The frozen slice is now implemented for positive `max_outstanding` values with
`ordering in_order` (validated at N=1, 2, and 4). A new immutable
`RequestResponseConnection` descriptor groups the two physical channel edges,
their payload types, directional buffer depths, ordering contract, and the
shared semantic identity. `HierarchicalConnection` remains the physical
ready/valid wiring record; no protocol state is flattened or inferred from RTL
names. Manifest and formal bindings publish the shared outstanding ledger and
the request/response occupancy/transfer identities with source provenance.

Admission and accounting are deliberately separate. A request FIFO enqueues
only when the requester asserts `valid` and the FIFO has capacity; responder
acceptance is the dequeue event and increments the parent outstanding counter.
Response buffering is independent, is blocked when there is no accepted
request, and decrements outstanding only when the requester consumes a
response. The parent owns this single counter, so the closed bundled Clash
child ABI remains unchanged and cannot diverge from buffered accounting.

`TransferEngine<AW>` and `MemoryModel` in `simple_dma_m40.zl` now use
`max_outstanding 2`, with `request_buffer 4` and `response_buffer 2` on the
hierarchical connection. The stateful engine is emitted as a reusable bundled
mealy component; its register/rule state remains in the child while protocol
accounting remains in the parent. Real Clash 1.11 generation, Verilator lint,
and the SimpleDMA simulation pass. Formal IR generation includes bounded
outstanding, no-response-without-acceptance, in-order, conservation, and reset
epoch property families; external proof execution remains subject to the
existing M35 harness/tool availability.

Zero/non-positive capacities and out-of-order ordering are rejected, generic
`buffer` remains ambiguous and rejected, and no transaction IDs or adapters
are introduced. The next concrete blocker is richer response matching and
observable multi-request traffic in a top-level DMA testbench; those are
outside this slice.

### 12. Standard-bus validation review

The next validation phase retained AXI4-Lite and APB as explicit
standard-library frontends around a small single-outstanding semantic RegBus;
they are not compiler-special protocols and SimpleDMA is not converted to AXI.
The review identifies two bounded prerequisites—structural protocol schemas and
compiler-shipped `std` imports—then recommends AXI4-Lite-to-CSR and APB-to-CSR
vertical slices. No compiler semantics were changed by the review.

### 13. Standard-bus library slice

The first control-plane library slice now provides deterministic imports for
`std.bus.reg`, `std.bus.axi_lite`, and `std.bus.apb`, plus backend-independent
RegBus, AXI4-Lite AW/W-join, and APB setup/access models. This work deliberately
does not change SimpleDMA's generic request/response memory interface or add a
bus adapter to it. The full library design and current limitations are in
`docs/standard-bus-library.md`; the emitted component ABI is now validated
separately, and the next concrete step is same-unit source hierarchy around a
CSR target.

The standard-bus frontend components now emit independently as closed bundled
Clash children and lint cleanly. This validates their ABI and public bindings
without converting SimpleDMA: its memory path remains generic
request/response. Source-level AXI/APB-to-CSR hierarchy is the next concrete
integration step.

The standard-bus integration now elaborates imported frontend and CSR target
instances through the ordinary hierarchy path. AXI and APB examples inherit
the parent clock/reset, connect a typed RegBus endpoint, emit real top-level
Clash designs, and pass Verilator reset simulation. SimpleDMA remains on the
generic request/response memory path; no AXI conversion was introduced.

The CSR validation slice now exercises the source-authoritative path with RW,
sticky W1C, and pulse state in `RegBusCSRTarget`. M35 bindings preserve the
frontend, RegBus, CSR state, instance, specialization, and aggregate member
identities. `zlang/standard_bus.py` is used only for trace comparison; it is not
called by production compilation. Real Clash/Verilator hierarchy tests and
failure-first formal mutation classification cover the supported safety subset.

### 14. Standard-bus source migration checkpoint

The built-in resolver now loads and parses `stdlib/bus/reg.zl`,
`stdlib/bus/axi_lite.zl`, and `stdlib/bus/apb.zl` as ordinary source modules,
retaining a logical source identity and content hash in typed IR. The Python
models remain independent verification oracles.

Migration stops at the first generic language blocker: the current grammar and
IR do not yet represent parameterized structural protocols or role-qualified
aggregate endpoints. Consequently the source files contain only the initial
profile declarations/stubs; AXI/APB behavior is still supplied by the legacy
backend emitter. No bus-specific backend path was removed prematurely. The
next implementation must add the generic endpoint capability before moving
AW/W joining, APB sequencing, CSR composition, manifests, or formal contracts
into authoritative `.zl` source.

The generic capability is now implemented structurally: parameterized protocol
members expand to typed hierarchical leaf connections, including forward and
reverse ready/valid channels and plain scalar members. Aggregate buffering,
adapters, and CDC remain rejected by design. SimpleDMA behavior was not changed
in this slice.

The standard-bus source migration now elaborates parameterized AXI/APB
aggregate endpoints through the same generic hierarchy. AXI/APB behavioral
frontends remain blocked on generic multi-ready/valid stateful child lowering;
no DMA or bus-specific backend workaround was introduced.

### 15. Recursive formal binding

The unnumbered recursive M35 formal infrastructure is documented in
[compositional-formal-binding.md](compositional-formal-binding.md).
It preserves nested DMA/CSR semantic instance identities and aggregate member
paths in a v4 manifest. This does not yet claim hierarchical M36 or M38
equivalence; that remains an explicit boundary.

### 16. Direct-SystemVerilog composition validation

The generic compositional direct-SV emitter now lowers the real scalar,
stateful, mixed-port, ready/valid FIFO, request/response, standard-bus, and
SimpleDMA hierarchies without module-name dispatch or placeholder substitution.
Each specialization is emitted once and each `ElaboratedInstance` remains a
physical child instance.  Ready/valid wires preserve forward payload/valid and
backward ready; connection buffers are real stateful FIFOs with simultaneous
push/pop behavior.

Real Verilator simulation passes for producer→FIFO→consumer stalls,
hierarchical request/response acceptance, and SimpleDMA reset/start/request
buffering/completion.  AXI/APB source-authored tops and every other accepted
example pass Verilator lint.  Real M35-oriented direct-RTL checks pass for
state/rules, FIFO/ready-valid, CSR W1C, and request/response acceptance; six
deliberate implementation mutations fail with solver counterexamples.

Transaction simulation also exposed a source-library response-lifetime bug:
the CSR target's one-cycle RegBus response could precede frontend readiness.
`stdlib/bus/reg.zl` now owns a one-entry response-hold register and retains the
response through transfer.  Real APB and AXI-Lite writes now complete through
the same generic hierarchy in direct-SV simulation; no backend special case was
added.

BackendArtifact v4 now carries direct-SV recursive physical locators for child
state and ports.  The next concrete formal blocker is an explicit recursively
bubbled formal-observation ABI for hidden state, particularly the parent-owned
multi-outstanding ledger.  Until that exists, recursive properties requiring
those ports remain explicit skips; no unconnected observation is reported as a
proof.
