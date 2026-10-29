<a id="reference-known-limitations"></a>
# Known limitations

This chapter describes the public `0.1.0a24` candidate surface.


ZLang HDL rejects unsupported combinations instead of generating partial RTL.
The following restrictions are part of the current user-facing contract.

<a id="reference-known-limitations-language-and-backend-boundaries"></a>
## Language and RTL boundaries

- ZLang has no runtime procedural `if`, mutable local variables, implicit
  numeric casts, implicit fixed-point rescaling, or general HLS scheduler.
- Integer `%` is compile-time only. Runtime and fixed-point remainder are
  unsupported; ports, registers and ordinary function arguments are not
  compile-time operands.
- Runtime-selected instance inputs/protocols, general cross-module atomic
  scheduling, fully qualified AXI4 endpoint compliance, automatic CDC insertion,
  and arbitrary stateful elastic pipelines are unsupported. Bounded source-owned
  AXI4 profiles with IDs and bursts are available under validation; consult the
  [standard bus contracts](standard-bus-library.md) rather than assuming complete
  endpoint compliance.
- Stateful objects inside one module may belong to independent explicit clock
  domains. A bounded `async_mem` is the sole storage exception: its one writer
  and one registered reader have distinct owners and form an explicit semantic
  boundary. Asynchronous 2RW memory, mixed widths, automatic banking,
  cross-clock atomic rules, derived/gated clocks, and target-aware scheduling
  across a CDC boundary are unsupported. Ordinary global memory control cannot
  be combined with unrelated user register/rule state in the same module; use
  hierarchy for that composition.
- Unsupported Direct-SV combinations produce a diagnostic rather than partial
  RTL.

## Other bounded combinations

The former combined guide's detailed exclusions remain part of this contract:

- Packed indexing and fixed-width `value[offset +: WIDTH]` need an unsigned
  index with a proven range. Variable-width/reversed slices, runtime reshape,
  heterogeneous-vector concatenation, enum `bitcast`/`pack`/`unpack` and external
  top enum inputs are unsupported.
- Traits, runtime polymorphism, generic methods and recursive generic
  programming are unsupported. Nominal overloads retain their actual typed
  bodies; the compiler does not assume algebraic laws from an operator symbol.
- Dynamic vector updates admit one range-proven element of a one-dimensional
  `reg vec<N,T>`, not arbitrary nested writes/ranges. Read-only output projection
  such as `lane[select].value` is distinct from selecting an instance input.
- ROM has no reloadable/asynchronous/multiport form or automatic exploration.
  Writable memory admits uniform compile-time initialization, not arbitrary
  per-address images. Named same-domain memory ports are bounded to eight;
  banking and arbitration remain explicit architecture.
- There is no package registry, mutable dependency revision or unlocked
  external project import. A standalone source file with no project/dependency
  manifest is a different, supported workflow.
- Named module interfaces check exact conformance; they do not automatically
  substitute implementations. Arbitrary cross-module implementation selection
  is not implied by the existence of an interface.
- General liveness/eventuality/fairness, arbitrary solver-specific temporal
  source properties and general hierarchical semantic-reference equivalence
  remain outside the supported proof relations.

<a id="reference-known-limitations-verification-boundaries"></a>
## Verification boundaries

Formal support is specific to each property family and observation boundary.
`bounded_pass` is not an unbounded proof, and `skipped`, `unavailable`, and
`unknown` make no proof claim. Report any accepted design that emits invalid
RTL as a correctness defect.

Community supports explicit passive taps and user-managed vendor debug wrappers.
It does not provide automatic hardware instrumentation or an ASIC scan/ATPG flow.
External tools and board capture are separately qualified; see the
[vendor example](../examples/fpga_debug/README.md).
