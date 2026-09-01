# Named module interfaces

Named module interfaces give a public name to an exact, behavior-free module
signature. They let a library state the ABI that several concrete modules must
implement without selecting, replacing, or instantiating any implementation.

```zlang
interface FirIfc {
    clock clk
    reset rst
    in  x : fixed<18,16>
    out y : fixed<18,16>

    timing {
        latency 4
        ii 1
    }
}

module DirectFir : FirIfc {
    // The complete public surface is inherited from FirIfc.
    y = pipeline(4) { x }
}
```

The declaration after `interface` contains only public signature items:

- type and value parameters;
- `clock` and `reset` declarations;
- scalar, wire, ready/valid, credit, packet, and VC-credit `in`/`out` ports;
- source-authored aggregate protocol endpoints and their roles;
- at most one exact `timing { latency N ii 1 }` contract.

It cannot contain assignments, registers, rules, storage, child instances, or
other implementation behavior.

## Exact conformance

`module M : Ifc` means that `M` promises to have exactly the applied signature
of `Ifc`. Conformance is checked during typed semantic analysis. It is not
structural duck typing and does not introduce an implicit conversion.

The following all belong to the contract:

- parameter names, kinds, declaration order, and defaults;
- applied type and value parameter values after specialization;
- port names, declaration order, directions, canonical types, and protocol
  capacities;
- clock/reset pairs and every port or endpoint domain;
- aggregate protocol specialization, role, member ownership, payload types,
  and member domains;
- presence and exact value of the module timing contract.

If a conforming module declares no public surface, the complete applied
interface surface is inherited: parameters, ports, clock/reset domains,
aggregate endpoints, and timing. This is exact signature inheritance, not
structural inference. The legacy fully redeclared form remains accepted. A
partial redeclaration is never merged with the interface and fails exact
conformance.

Missing or additional members in a fully redeclared surface are errors. So are renamed ports, changed
directions, width changes, role changes, a different domain, and a timing
contract present on only one side.

Named parameters and concrete type parameters are supported:

```zlang
interface TransformIfc<type T, N=4> {
    in  x : vec<N,T>
    out y : vec<N,T>
}

module Transform<type T, N=4> : TransformIfc<T=T, N=N> {
    y = x
}
```

The module parameter declaration itself must match the interface parameter
declaration exactly. A concrete specialization records canonical applied
values in `ModuleSignature`; concise source spelling does not change that
identity.

## Aggregate protocol signatures

An existing source-authored aggregate protocol can be part of a named module
interface. Its role and clock domain are explicit:

```zlang
protocol TinyBus<AW=8> {
    role initiator
    role target
    channel command : rv<bits<AW>> initiator -> target
    member alarm : bit target -> initiator
}

interface TinyTarget<AW=8> {
    clock clk
    reset rst
    interface bus : TinyBus<AW=AW>.target @clk
}

module Target<AW=8> : TinyTarget<AW=AW> {
    clock clk
    reset rst
    interface bus : TinyBus<AW=AW>.target @clk
    // Concrete behavior remains in the module.
}
```

Conformance uses the already typed aggregate schema and ownership model. It
does not guess compatibility from endpoint or generated RTL names.

## Selection and backend identity

A named interface never chooses a concrete implementation. Source still
instantiates a concrete module, and normal top selection still names a concrete
module. There is no automatic substitution, inheritance, refinement search, or
runtime dispatch.

The applied `ModuleSignature` is backend-independent semantic/build metadata.
It survives canonical round trips, while the implementation body continues
through the existing Clash and direct-SystemVerilog paths. Merely adding an
equivalent interface declaration does not authorize a backend to change RTL or
timing.

## First-slice boundaries

The current bounded surface intentionally does not support:

- `request_response` members in named module interfaces, because their
  requester/responder role is currently inferred from module behavior rather
  than declared as an exact signature role;
- interface extension or refinement beyond exact complete-surface inheritance;
- multiple-interface conformance;
- automatic implementation selection or substitution;
- relaxed variance, implicit resizing, protocol adaptation, or CDC insertion.

Use the existing concrete module and protocol forms when one of these
boundaries applies. Unsupported or non-conforming signatures fail with a
structured interface diagnostic; they never silently weaken the ABI.
