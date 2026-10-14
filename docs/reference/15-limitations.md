<a id="reference-known-limitations"></a>
## Current limitations

This chapter collects the main boundaries of the current release. Earlier chapters describe the supported subset at the point of use.

### Language and scheduling

- Implementation exploration is bounded and does not claim a global optimum.
- Ordinary scalar implementations are fixed-rate II=1. Automatic II>1 sharing is limited to the documented capacity-one ready/valid `a*b + c*d` form.
- There is no general HLS scheduler, modulo scheduling, arbitrary transaction overlap, or hidden retiming.

### Simulation and verification

- Native four-state diagnostics model `0`, `1`, `U`, and `X`, not electrical `Z`, drive strength, or the Verilog event-region model.
- FST/VCD traces use an event-index timeline rather than analog delay timing.
- Bounded model checking yields `bounded_pass`, not `proven`. Formal support is property- and structure-dependent; unavailable routes establish no proof claim.

### CDC, storage, and physical targets

- Clock-domain crossings require explicit supported crossing constructs. The compiler does not infer CDC intent.
- Memory latency, collision, masking, and port semantics must match a supported declaration and target mapping.
- Frequency intent filters evidence-backed candidates but does not itself create a clock constraint or prove timing closure.
- Generated XDC/SDC support is deliberately narrower than language-level multi-clock semantics.

### Protocol and library boundaries

- Protocol conversion is explicit; no ready/valid, credit, request/response, or bus adapter is inserted silently.
- Standard buses are bounded typed library facilities. Full arbitrary AXI4, dynamic topology generation, and implicit external-IP binding are outside the current surface.

ZLang HDL rejects unsupported combinations instead of generating partial RTL.
The following restrictions are part of the current user-facing contract.

<a id="reference-known-limitations-language-and-backend-boundaries"></a>
### Language and RTL boundaries

- ZLang has no runtime procedural `if`, mutable local variables, implicit
  numeric casts, implicit fixed-point rescaling, or general HLS scheduler.
- Runtime-selected instance inputs/protocols, general cross-module atomic
  scheduling, full AXI4, automatic CDC insertion, and arbitrary stateful
  elastic pipelines are unsupported.
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

<a id="reference-known-limitations-verification-boundaries"></a>
### Verification boundaries

Formal support is specific to each property family and observation boundary.
`bounded_pass` is not an unbounded proof, and `skipped`, `unavailable`, and
`unknown` make no proof claim. Report any accepted design that emits invalid
RTL as a correctness defect.
