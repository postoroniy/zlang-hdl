# Recursive M35 formal binding implementation

Status: the bounded register, FIFO, in-order request/response, and CSR semantic
state families are implemented for both backend manifests where their exact
observations are published. Missing observations still produce explicit skips;
arrays and hierarchical M36/M38 remain outside the slice. A later concrete
Wi-Fi controller requirement connected the already-existing rule exclusivity
and priority properties to formal-only accepted-fire observations in both
backends. That closure leaves production RTL unchanged and does not create a
new property family. Early “remaining Clash transport” and rule-fire wording
below records the original checkpoints.

The recursive formal slice uses the backend-independent implementation boundary
below. Current applicability and fail-closed behavior are summarized in
[Optimization and formal verification](optimization-formal.md).

```text
typed/elaborated hierarchy
        -> RecursiveFormalDesign
        -> BackendArtifact v4 recursive manifest
        -> backend formal observation artifact
        -> one whole-top M35 harness
```

`RecursiveFormalDesign` records component contracts, physical instance nodes,
specialization identities, semantic object references, concrete M35 properties,
aggregate member paths, domains, and source origins. The property identity is
derived from source property, module/source hash, specialization, physical
instance identity, and recursive schema. Same-specialization siblings are
therefore separate formal targets.

Manifest v4 is additive: v2 and v3 artifacts remain readable. It adds component
and instance tables, recursive semantic bindings, formal observation records,
and formal-artifact identity. The production Clash artifact remains unchanged.
The Clash formal adapter now emits a separate `*_formal` top entity using the
closed bundled ABI: the original top output is bundled with typed observation
signals, and the Synthesize annotation exposes deterministic observation ports.
Protocol-ledger observations that are already explicit top-level signals (for
example request/response outstanding and transfer events) are materialized and
published with v4 locators. Nested child registers and other state that the
current Clash component ABI does not export remain explicit unavailable
observations; they are not replaced with guessed names or unconstrained
placeholders. The generated formal artifact has its own content hash and keeps
the production RTL ABI untouched.

The adapter has been compiled with Clash 1.11 and its generated
`SimpleDMA_formal` Verilog passes Verilator lint. The recursive runner still
returns `skipped` for any required observation that is not connected, and never
claims a proof from incomplete instrumentation. RTL names are backend locators
only and are never parsed back into semantic hierarchy.

The first execution strategy is a whole-top harness. Hierarchical M36,
compositional theorem decomposition, direct-SystemVerilog emission, liveness,
CDC, memory refinement, and bus-specific formal paths remain excluded.

## Clash nested user-register slice

The first structured Clash formal-component slice is implemented for scalar
user-register hierarchy. Formal emission is generated deliberately from typed
and elaborated IR; it no longer extends the transitional top-source rewrite for
this class. Each physical instance has a deterministic NOINLINE formal
component returning its normal functional output plus a flat subtree register
observation record. Record leaves contain the actual current committed register
Signals, not next-state expressions.

Same-specialization siblings receive distinct functions derived from physical
instance identity, preventing Clash 1.11 from merging identical-input stateful
instances. A depth-two register and two sibling counters compile into separate
Clash RTL components and separate top observation ports.

The emitted v4 manifest is initially fail-closed. Register observations become
`physical_available` only after real Clash output is checked for the allocated
top port and exact width. The finalized formal artifact hash covers the
generated RTL set. Missing ports and width mismatches remain unavailable.

Recursive register range/reset properties now execute through SBY/Z3. A
child1-only reset mutation fails with a counterexample attributed to child1's
physical instance, specialization, semantic register, property, source origin,
and failing cycle; the identical child0 instance remains passing. Clash and
direct-SV publish the same semantic register-observation set for the shared
fixture.

## Clash nested FIFO slice

The same structured component ABI now transports nested FIFO semantic state.
The observation inventory is deliberately limited to the existing M35-visible
objects: committed `count`, accepted `push`/`pop`, `empty`, `full`, and `front`.
The Clash mapping binds semantic `push` and `pop` to the implementation's
accepted `enqueue` and `dequeue` signals. Request attempts, `count_next`, FIFO
slots, and implementation pointers remain private.

Depth-two FIFO hierarchy and two same-specialization siblings compile as closed
NOINLINE Clash components with distinct physical-instance identities. Generated
RTL passes Verilator lint and simulation for reset, fill, full/empty, pop,
simultaneous push/pop, drain, and reset with buffered data. BackendArtifact v4
availability remains false until the formal top port and exact width are
validated against real generated RTL; JSON round trips retain those locators.

The recursive runner executes the existing FIFO bounds, accepted-transfer
legality, conservation, and non-popped-front stability properties through
SBY/Z3. Validation found and
fixed an old property mismatch: a full FIFO may accept a push when a
simultaneous pop frees capacity, so `no_push_full` includes that exception. A
mutation that breaks the decrement transition only in sibling `fifo1` fails
with an attributed counterexample while `fifo0` remains bounded-pass.

Direct-SV now emits the same scalar-controlled FIFO child through its generic
composed storage path. The existing recursive FIFO observation family is
therefore published for that artifact too; this is reuse of the frozen M35
family, not a new formal abstraction. Request/response, CSR-derived state, and
rule-fire observations retain their separately documented applicability and
execute only when every required binding is published.
The completion regression is 618/618 tests, including real Clash 1.11,
Verilator, SymbiYosys/Yosys, and Z3 execution.

## Clash nested request/response slice

The structured component ABI now also transports the already-frozen in-order
request/response ledger. Its semantic leaves are committed `outstanding`,
responder-side `request_accept`, requester-side `response_consume`, and the
independent request/response buffer occupancies. Candidate next-state,
`waiting_response`, buffer storage, and other backend helpers are not exposed.

Occupancy types follow their physical semantic capacity: a buffered channel
uses its buffer-depth count width, while an unbuffered channel's constant-zero
observation uses the ledger width. Availability is still finalized only after
the generated formal-top port and exact width are validated. Same-specialization
siblings use distinct NOINLINE physical wrappers and deterministic top ports.

Real SimpleDMA validation demonstrates that request admission can increase
request occupancy without changing outstanding, responder acceptance increments
outstanding, response buffering does not decrement it, requester consumption
does decrement it, and simultaneous acceptance/consumption preserves the count.
Reset clears all three committed quantities and starts a new transaction epoch.
All five exact recursive request/response M35 properties pass through SBY/Z3.
A child1-only mutation that suppresses the connection ledger's accepted-request
increment fails with an attributed counterexample while child0 remains
bounded-pass.  Mutating requester/responder child-local bookkeeping is not a
valid ledger mutation: the typed hierarchical connection owns the authoritative
accepted-request accounting state.

This slice corrected three concrete pre-existing defects found by real
instrumentation: `no_response_without_request` now references response
consumption, buffer occupancy bindings use their actual widths, and Clash
explicitly resizes response occupancy for `waiting_response`. At that historical
checkpoint direct-SV had not yet published accepted-transfer or directional-
buffer occupancy observations. The current backend publishes those typed
observations and the parent-owned outstanding ledger, so the supported recursive
request/response properties execute through both backends.

CSR and rule-fire transport were closed by later bounded real-design slices.
Arrays that do not publish complete observations and hierarchical M36/M38 remain
outside the current formal boundary.
The completion regression is 625/625 tests.

## CSR semantic-state follow-on

Recursive M35 now addresses CSR storage by instantiated `CsrFieldIdentity` and
resolves it through `CsrFieldStateBinding.implementation_state_id`; it never
searches for `rw_state`, `w1c_state`, `pulse_state`, or an RTL spelling. The
formal-only component projection carries committed state, accepted decoded
write hit, and selected write bits. All six RW/W1C/pulse reset and update
properties pass real depth-6 BMC. W1C and pulse mutations fail with cycle
traces, and an instance-local mutation leaves its same-specialization sibling
passing. Clash and direct-SV publish the same nine semantic observations.
Generated AXI4-Lite and APB formal tops validate all nine leaves through the
source-authored frontend -> RegBus target -> CSR bank hierarchy.
The completion regression is 639/639 tests.

## Structured-predicate correctness repair

Executable M35 properties now use the versioned structured predicate IR. The
human-readable `expression` field is no longer parsed by either recursive
runner. Each observation is resolved by exact semantic ID, width, signedness,
clock domain, reset domain, and a backend-published locator. Protocol aggregate
bases are deliberately nonphysical; only their explicitly owned leaves may be
connected.

Connection is property-specific and fail-closed. A missing observation makes
the affected property non-executable without disabling unrelated properties.
Recursive Clash execution validates every instance/property/observation domain
against the connected root before mapping the closed component ABI. At the
structured-predicate checkpoint, receiver-credit accounting was unavailable
because its occupancy observation was not yet published. Both backends now
publish and bind that observation, and the sender and receiver families pass
real SBY/Z3.

The structured-predicate repair was accepted at 1177 tests; the live repository
baseline is recorded in the README and backend-tooling guide. The
formal-infrastructure freeze remains in force: additional rule-fire or state
observation families, hierarchical M36/M38, and compositional theorem machinery
require a concrete real-design correctness need.
