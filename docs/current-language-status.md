# Current ZLang HDL language and implementation status

This is the current-facing status snapshot for the implemented language. It is
maintained alongside the executable [syntax support matrix](syntax-support-matrix.md)
and compiler-owned `zlang.public_capabilities.CAPABILITY_REGISTRY`.
Milestone reports, design freezes, architecture reviews, QoR tables, and the
coordination logs are historical evidence: their original scope, tool results,
and test counts remain valid for the recorded slice but are not the current
repository baseline.

Snapshot date: **2026-09-08**.

The bounded formal-closure and concise-lowering follow-up is accepted in this
snapshot. Its current public behavior is documented in
[Optimization and formal verification](optimization-formal.md) and
[Expressions, functions, and generics](expressions-functions-generics.md).

The superseding exact-reset formal-applicability slice is accepted in this
snapshot. Existing formal routes now consume the physical contract already
carried by `ClockDomain` and BackendArtifact v10; it adds no source syntax,
property/observation family, or new equivalence relation.

## Validation baseline

- The public release minimum, zero-skip policy, corpus totals, and exact EDA
  versions are machine-readable in
  [`release/status.json`](../release/status.json). Release CI validates two
  complete eight-worker JUnit reports against that manifest. This includes
  compiler-owned formal closure, concise lowering, specialization-safe M39,
  exact physical reset applicability, and real external-tool integrations.
- Exhaustive example registry, updated with the verification tutorial on
  2026-09-08: **88 `.zhl` files / 178 module roots / 161
  standalone roots / 17 child or template roots / 0 unsupported roots**.
- Every standalone direct-SystemVerilog root emits a `BackendArtifact` and
  passes strict Verilator lint; child/template roots are exercised through a
  concrete parent.
- The accepted tool host has Verilator 5.044, Yosys and SymbiYosys 0.68,
  `yosys-smtbmc`, and Z3 4.8.12. GHC and Clash are not installed or discovered
  by the compiler and are not test or release dependencies.
- The complete 1,033-cycle FFT512 persistent-hierarchy replay runs by default,
  accepts all 1,023 offered tokens, and produces the frozen 512-output digest.
- Direct-SV simulation tooling can publish a separate semantic state catalog
  and Verilator VPI header for exact register/vector/writable-memory preload
  and inspection. It does not add RTL ports or change the production artifact.

These counts describe the checked-in corpus and accepted regression, not a
promise that every future combination of typed IR is supported. Backends and
verification routes remain fail-closed.

## Backend policy

Direct SystemVerilog is the sole production backend. Historical Clash support
has been removed from the compiler, package, CLI, tests, CI, and release gate.
Direct SystemVerilog is the sole supported production backend for its validated
subset; `--systemverilog` is the public option and
`--experimental-systemverilog` is a compatibility alias. The backend does not
define ZLang semantics: it consumes backend-independent typed IR, shares the
public `TopPhysicalABI`, and publishes versioned `BackendArtifact` bindings.

Top-level struct fields, tuple `itemN` components, and protocol aggregates are
exposed as semantic leaves in direct SystemVerilog. Vectors remain native public
arrays. Direct-SV component ABIs are closed over all child dependencies;
unsupported combinations cannot publish partial RTL.

## Implemented language surface

The current language includes:

- exact scalar integers/raw bits, signed and unsigned fixed point, explicit
  resize/rescale/rounding/overflow, characters, fixed byte strings, structural
  tuples, vectors, nominal structs, encoded enums, tagged unions, slices,
  concatenation, reshape, packing, and LSB-zero compile-time packed-bit indexing;
- immutable inferred bindings, pure ordinary and generic functions, inferred
  returns, generic structs and nominal arithmetic operators, exact typed
  constant/function parameters, compile-time constraints, deterministic
  generation and functional `map`/`reduce`/`sum`/`dot` regions;
- registers, exact delays and fixed pipelines, concise enum FSMs, atomic guarded
  rules with recursive runtime `when`/`else when`/`else`, one outer `Rule` and
  `ActionGroup` identity, active-effect and output-conflict scheduling, and
  explicit priority; runtime-indexed one-dimensional vector-register updates,
  FIFOs,
  1R1W writable memories with global zero/one-cycle reads,
  independent cell/read-result reset policy and one-cycle rule-local actions,
  source-composed replicated read ports through compile-time child arrays, and
  immutable initialized synchronous ROMs;
- parameterized hierarchy, named module interfaces, bounded external modules,
  scalar/mixed/protocol children, instance arrays, read-only runtime instance
  output projection, ready/valid, credit, request/response, packet/VC,
  arbitration, CSR, aggregate protocols, the source-owned bounded ZTPU AXI
  burst profile with single-outstanding read/write helpers, and explicit CDC;
- default synchronous reset, raw asynchronous-reset compatibility, and concise
  asynchronous assertion with one root-owned two-edge synchronized release;
- canonical optimization IR, bounded exact scalar arithmetic equality
  saturation, exact-N pure-DAG scheduling with reconvergence balancing,
  Xilinx 7-Series DSP48E1 covering, explicit and automatic
  architecture/pipeline exploration, target/resource descriptions,
  implementation profiles, reproducible projects, source maps, evidence reports,
  and whole-build manifests;
- named same-cycle `assert`/`ensure`, scoped `require`, bounded `cover`, legacy
  `assume`/`guarantee`, immutable verification bundles, and real external solver
  execution for connected supported routes.

The exact syntax, context, witness, and phase-specific limitations are in the
[support matrix](syntax-support-matrix.md). The product-first entry point is the
[language guide](language-guide.md); `examples/all_syntax.zhl` is a representative
tour, not an exhaustive language specification.

## Formal and optimization status

- **M35** executes real safety checks for the supported register, enum,
  FIFO, ready/valid, credit, CSR, request/response, and rule observations.
- **M36** provides authoritative canonical-reference equivalence for the frozen
  scalar and fixed-latency II=1 candidate classes.
- **M38** cross-backend equivalence is retired with Clash. Historical M38
  records remain dated evidence only; current verification uses M35 safety and
  M36 semantic-reference equivalence.
- **M39** gates supported exploration candidates using `off`, `available`,
  `required_bmc`, or `required_proven`, deterministic rank order, and a
  content-addressed proof cache.
- Compiler-owned formal orchestration plans each M35 goal independently with
  its exact assumptions/domain/observations, uses the direct-SV route and the
  backend-independent semantic reference, retains explicit comparison windows
  for M36, memoizes prepared artifacts in one compilation session, and can
  reuse exact hash-validated prepared/result records from `--formal-cache`.
  `--formal-jobs` parallelizes bundled safety/cover jobs and independent
  selected-candidate sites. Within one site, the direct-SV M36 leg and
  BMC-before-prove dependencies remain ordered.
- Goal routing is clock/reset-domain local. Multiple supported synchronous
  domains can contribute independent jobs. In a single-domain module, existing
  formal routes also support rising/falling edges, synchronous or raw
  asynchronous assertion, either polarity, and the concise two-edge
  synchronized release, with `power_up unspecified`. An unsupported domain
  skips only its own goals. This does not add cross-domain temporal semantics
  or promote general multi-domain backend support.
- Exact physical-domain identity is retained across BackendArtifact v10,
  `FormalExecutionPlan` schema 2, bundle v4 / verification-IR snapshot v3 jobs,
  and run-report v7 results. Plan/job/result restoration rejects a mismatched
  contract or domain digest, and result/cache identities separate edge,
  assertion, polarity, and release-policy changes.
- Trace data distinguishes the raw polarity-preserving `physical_reset` from
  normalized active-high `trace:reset`. Property guards, history/fill masks,
  comparison windows, and result `reset_state` use the effective reset; during
  synchronized release it remains active for both release edges after raw pin
  deassertion.
- The execution triggers are distinct. A non-`off` formal policy alone runs the
  M39 selection-time M36 route. `--verify` with policy `off` runs M35/source
  safety and covers. Bundle-only publication does not prepare or execute
  selected-candidate M36. Joint `--verify` plus a non-`off` policy also runs
  compatible direct-SV M36 evidence. When publication has
  prepared those exact selected-candidate routes, the immutable bundle carries
  strict path-free replay companions and `zlang-verify` can execute them later
  without source compilation or M39 reselection. A base safety/cover bundle
  still contains no candidate route.
- Raw safety/cover execution uses `zlang-verification-run-report-v7`; joint
  candidate execution uses `zlang-compiler-verification-report-v1` as a wrapper
  that preserves the distinct M35 and M36 result types. Proof always
  follows a clean safety BMC stage; covers run once and are not rerun.
- Joint candidate reports retain deterministic per-route work roots and the
  tool snapshot when execution discovers tools. Exact in-session M39 reuse
  carries its recorded root; persistent cache data excludes physical paths.
- Historical M38 records are advisory and retired; M38 never gates M39.
- An executed M36 counterexample fails joint verification. Unavailable
  advisory candidate evidence does not change M39 eligibility or make an
  otherwise complete run fail. Missing/unknown/vacuous M35/source evidence
  remains explicitly incomplete.
- BMC success is always `bounded_pass`; only a complete unbounded safety proof
  is `proven`. A cover miss is `bounded_unreached`, never a proof of
  unreachability.
- Recursive register/FIFO/request-response/CSR goals execute only with complete
  published observations. Requirement ownership is traced through typed
  hierarchy to the root ABI. A truly external recursive assumption set receives
  one deduplicated feasibility cover. A compiler-generated ready/valid
  requirement driven by one direct same-domain implementation endpoint is
  checked monolithically on the root RTL: upstream guarantees are assertions,
  dependent goals are requirement-guarded, and no internal requirement becomes
  an `assume` or an assumption ID. Including the independent root feasibility
  cover, this yields 27/27 executable jobs for `IeeeMapper64Verification` and
  5/5 for `IeeeIFFTBoundaryVerification`. User-authored requirements and buffered,
  adapted, crossing, mixed, unresolved, or unbindable ownership remain
  explicitly incomplete. An assumption is never dropped to make a goal
  executable.
- Existing rule exclusivity and priority properties use formal-only accepted-
  fire observations in direct-SV. They are derived from the one typed
  resolved schedule and do not alter production RTL text, ABI, or hashes. This
  closes the concrete existing rule family; it does not open a new observation
  family or source temporal semantics.
- Existing request/response accounting can bind the parent-owned outstanding
  ledger and independent request/response buffer occupancies through typed
  formal-only component ports in both backends. Existing receiver-credit
  accounting likewise binds the real adapter occupancy, send, and returned
  credit; direct-SV is the production route. Every non-empty automatic root assumption set receives
  an unassumed feasibility query, so an otherwise clean safety BMC cannot pass
  only because the environment contract was impossible.
- Same-cycle source properties may use a runtime vector read only after the
  ordinary semantic range proof succeeds. The formal predicate preserves the
  MSB-first packed vector layout and uses the existing mux/equality vocabulary;
  an assertion never supplies the range proof.
- One deliberately bounded whole-root equivalence helper covers exactly one
  combinational scalar child. It materializes the semantic value from typed
  hierarchy and instance bindings, then obtains decisive direct-SV M36
  evidence from separately namespaced
  formal-only artifacts. Production hierarchy is not flattened, and state,
  storage, protocols, arrays, aggregates, and nested hierarchy remain rejected.
- Egglog is limited to exact pure scalar value rewrites. It does not schedule
  state, protocols, or pipeline placement. Protocol-only `transform
  pipeline(auto)` uses the separate
  candidate/planner path; the bounded elastic transform has explicit
  ready/valid stall semantics and no M36 claim.

The formal-infrastructure freeze remains active. The bounded one-child value
helper above reuses the existing same-cycle M36 relation and is not a
general hierarchical refinement system. VC-credit accounting, executable M33
buffered/variable-latency protocol equivalence, elastic M36, stateful or
nested hierarchy, CDC refinement, liveness/fairness, hidden memory cells, and
additional rule-fire families require a separate real-design freeze.

The applicability boundary and accepted transport rules are collected in
[Optimization and formal verification](optimization-formal.md).

Focused exact-reset validation reports **53 passed** for domain-plan/bundle
identity, **65 passed** for adjacent orchestration/replay, and **26 passed** for
physical-manifest/async-reset coverage. The sets overlap and are not summed.
`power_up reset`, multi-domain asynchronous reset, CDC/reset-refinement,
elastic/variable-latency equivalence, target DSP/BRAM reset pins, general
hierarchical M36/M38, and incomplete bindings remain explicit fail-closed
boundaries.

## Standard library and real designs

The compiler-shipped `std` namespace is resolved recursively from ordinary
source under `stdlib/`. It currently includes fixed and Complex math, stream
core/serialization helpers, FFT helpers, storage/ROM wrappers, coding helpers,
RegBus, AHB-Lite, AXI4-Lite, the bounded no-ID ZTPU AXI burst profile, APB, AXI-Stream,
Wishbone B4 Classic, and generic/ASIC/Intel/Xilinx target and architecture
descriptions. Bus behavior remains source-authoritative `.zhl`; Python bus
models are independent oracles.

The ZTPU profile publishes combined and separate read/write views over AR/R and
AW/W/B, plus aligned single-outstanding reader/writer helpers. The 64/32 witness
passes semantic/canonical restoration, deterministic direct-SystemVerilog
artifacts, simulator traces, and Verilator. Focused acceptance covers
1–256-beat counting, independently stalled channels, stable owned payloads,
counted final-beat handling, deterministic R/B error latching, and reset. It is
a bounded full-width incrementing subset, not a claim of full AXI4.

Accepted real-design evidence includes SimpleDMA, source-authored bus-to-RegBus
CSR tops, streaming packet/FIR/multi-channel DMA examples, fixed FIR and DSP48
experiments, a nine-stage FFT512 SDF reference, and the canonical ten-file IEEE
802.11a transmitter hierarchy. The Wi-Fi project contains no executable
`legacy_` or `production_` alternate path and uses direct packed-bit selection
instead of representation-only `vec<N,bit>` views.

## Explicit boundaries

The current product does not claim general procedural HLS or runtime
procedural `if`/`else`. Recursive action `when` is supported as atomic effect
selection, while compile-time `if` remains elaboration-only. The product also
does not claim mutable locals,
implicit numeric conversion, runtime polymorphism/traits, arbitrary runtime or
nested state selection, runtime-selected instance inputs or protocol endpoints,
native multiport writable memories, synthesizable writable-memory initialization,
automatic BRAM inference, implicit
CDC/adapters, full AXI4 IDs/multiple outstanding/general burst attributes,
arbitrary temporal/SVA/SMT source syntax,
true liveness/fairness, or general hierarchical M36/M38. Exact narrower forms
listed in the support matrix remain supported.

Work after the current numbered roadmap is tracked as bounded, evidence-driven
unnumbered slices rather than creating another numbered milestone.

## Documentation authority

For the public release, use sources in this order when statements differ:

1. executable compiler behavior and tests;
2. the compiler-owned capability registry and
   [syntax support matrix](syntax-support-matrix.md);
3. this snapshot and the product-first live guides;
4. the published validation and backend reports for bounded empirical results.

Maintainer-only historical design records and coordination logs are deliberately
excluded from the slim public repository and are not required to interpret the
released language or compiler.

Historical documents are intentionally not rewritten to replace old test
counts, tool availability, blockers, or measurements with later results. A
current-facing limitation that has since been closed should instead link here
or carry an explicit supersession note.
