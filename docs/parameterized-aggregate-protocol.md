# Parameterized aggregate protocol composition

The generic composition slice expands a parameterized protocol endpoint during
semantic elaboration. For example:

```zl
protocol TinyBus<AW=8> {
    role initiator
    role target
    channel req : rv<uint<AW>> initiator -> target
    channel rsp : rv<uint<AW>> target -> initiator
    member irq : bit target -> initiator
}
```

A module uses this declaration with
`interface bus : TinyBus<AW=8>.initiator`. The
compiler retains the aggregate identity and specialization, then expands each
member into the existing typed leaf endpoint IR.  Ready/valid members preserve
physical backward-ready propagation; plain members are ordinary scalar wires.

Top-level aggregate endpoints additionally have a backend-independent
`TopAggregateABI` projection. It recursively flattens ready/valid payload
structs, derives physical direction from source/sink ownership, and keeps
aggregate/member identities separate from generated names. Clash packs these
flat ports into the existing closed hierarchy and unpacks the result without
protocol-specific emitter logic.

`connect left.bus -> right.bus` checks protocol identity, specialization,
member names, payload types, roles, and clock domains before producing leaf
hierarchical connections.  Aggregate buffering, adapters, and CDC are
deliberately rejected; they must be expressed on a leaf connection in a later
library design.

The Clash backend consumes the same closed child component ABI as other
hierarchical protocol children.  A child receives every scalar dependency and
protocol backward signal explicitly and returns forward values plus scalar
outputs.  No aggregate or RTL name is reconstructed by textual substitution.
BackendArtifact manifests publish both aggregate identities and their leaf
signal bindings, so formal/source attribution remains stable.

Generic value parameters support exact positive width arithmetic (`+`, `-`,
`*`, and exact `/`).  Type parameters are bound at an aggregate use site.  The
initial slice is structural: it intentionally does not implement AXI/APB
transaction state machines, adapters, packages, CDC, or automatic protocol
conversion.

Validation: the in-repository TinyBus producer/consumer hierarchy (two
ready/valid channels plus a reverse scalar member) elaborates into three leaf
connections, emits reusable Clash child functions, compiles with Clash 1.11,
and passes Verilator 5.044 lint on the generated RTL.

The standard-bus source migration validates a stateful child with multiple
independent ready/valid channels through Clash 1.11 and Verilator. The formerly
missing top-level aggregate exposure is also implemented: an unconnected
aggregate bus on the selected top is projected through the shared
`TopPhysicalABI` into typed public leaves for both RTL backends. This is generic
schema/ownership lowering, not an AXI semantic exception. Arrays, partial
aggregate exposure, and unsupported protocol kinds remain fail-closed as listed
in the live [syntax matrix](syntax-support-matrix.md).
