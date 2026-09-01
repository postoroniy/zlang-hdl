# ZLang user guide and language reference

This is the stable entry point for ZLang documentation. The guide is split by
the questions a hardware designer asks while moving from source to verified RTL.
The compiler's backend-independent typed IR, rather than Clash or SystemVerilog
behavior, defines language semantics.

For a representative executable tour, start with
[`examples/all_syntax.zl`](../examples/all_syntax.zl). It is not an exhaustive
capability manifest. The [syntax support matrix](syntax-support-matrix.md) and
compiler-owned capability registry record supported, bounded, and deferred
forms; the VS Code grammar is lexical assistance, not semantic validation.
The [test strategy](testing.md) describes the one-item positive conformance
gate, the broad real-tool smoke gate, and the separate complete regression.
The dated [current language and implementation status](current-language-status.md)
records the accepted regression/corpus/tool snapshot and explains how current
guides relate to historical milestone and design-freeze evidence.

## Product-first guide

1. **[Getting started](getting-started.md)** — install, check, compile, choose a
   top, generate RTL, and understand concurrent assignments.
2. **[Types and numerics](types-and-numerics.md)** — scalar families, exact width
   rules, fixed-point policies, vectors, structs, and explicit conversion.
3. **[Expressions, functions, and generics](expressions-functions-generics.md)**
   — operators, selection, compile-time evaluation, functional datapaths,
   monomorphized generics, and nominal operator overloads.
4. **[Sequential logic, rules, and storage](sequential-state-storage.md)** —
   clocks/reset, registers, delays, pipelines, atomic rules, FIFOs, and memories.
5. **[Hierarchy, protocols, and composition](hierarchy-protocols.md)** — module
   specialization, instance arrays, ready/valid, credit, request/response,
   aggregate standard buses, CSR, arbitration, and CDC.
6. **[Optimization and formal verification](optimization-formal.md)** — canonical
   versus selected IR, choices, pipeline/architecture exploration, contracts,
   M35/M36/M38/M39 boundaries, and proof status meanings.
7. **[Backends, CLI, and tooling](backends-tooling.md)** — Clash/direct-SV policy,
   output options, reports, stdlib resolution, editor support, and test tooling.
8. **[Standard library](stdlib.md)** — the compiler-shipped `std` namespace,
   source-authored buses/math, targets, and architecture descriptions.
9. **[Projects and dependencies](projects-dependencies.md)** — logical imports,
   versioned manifests, pinned locks, path/Git dependencies, and offline builds.
10. **[Implementation profiles](implementation-profiles.md)** — external
   backend/target policy, semantic regions, conflict rules, and independent
   backend plans.
11. **[Named module interfaces](named-module-interfaces.md)** — exact reusable
    module signatures, parameter/domain/protocol/timing conformance, and the
    boundary between an interface contract and concrete implementation choice.
12. **[Whole-build manifests and evidence](whole-build-manifests.md)** —
    reproducible canonical/backend identities, published files, tool executions,
    and exact proof-status meanings.
13. **[Current implementation status](current-language-status.md)** — accepted
    corpus/test/tool snapshot, backend/formal policy, real-design evidence, and
    current explicit boundaries.

Compiler and editor integrations should also use the versioned
[structured-diagnostic and source-provenance model](structured-diagnostics.md).

## Quick language map

| Need | Current spelling | Detail |
| --- | --- | --- |
| Scalar hardware | `bit`, `u8`, `s16`, `bits<32>` | [Types](types-and-numerics.md#scalar-families) |
| Fixed point | `SF8.8`, `fixed<16,8>`, `quantize` | [Fixed point](types-and-numerics.md#fixed-point) |
| Characters and fixed strings | `char`, `string<N>`, `'A'`, `"OFDM"` | [Types](types-and-numerics.md#characters-fixed-strings-and-tuples) |
| Aggregates and control state | `[a,b]`, `repeat(value)`, structural `(a,b)`, flat tuple/struct destructuring, `struct`, immutable `with`, `enum State`, `union Message`/`match` | [Types](types-and-numerics.md), [tagged unions](tagged-unions.md) |
| Bit and collection layout | `x[MSB:LSB]`, LSB-zero `bits_value[i]`, exact `zeros<N>`/`ones<N>`, `concat`, `reshape`, `bitcast<T>`; low-level `pack`/`unpack<T>` | [Slicing and representation](types-and-numerics.md#slicing-concatenation-and-representation) |
| Pure reuse | `fn` with explicit or inferred exact return, generic `type T`, declaration-ordered defaults such as `IW=index_width(N)`, exact constant parameters, static `operation=fn name`, operators | [Functions/generics](expressions-functions-generics.md) |
| Functional datapath | `generate`, `map`, `reduce`, `sum`, `dot`, static `values[first..past_last]` | [Functional datapath](expressions-functions-generics.md#functional-datapath) |
| State | `reg`, `<-`, `when`, `priority a > b > c`, typed or qualified-initial `fsm` | [Sequential logic](sequential-state-storage.md) |
| Physical reset | `reset rst`; `async reset arst @clk` for two-edge synchronized release | [Clock/reset contract](physical-clock-reset-contract.md) |
| Storage | `fifo`, global or rule-owned synchronous `memory` with optional byte write masks, initialized `rom` | [Sequential storage](sequential-state-storage.md) |
| Hierarchy | `inst`, compile-time `inst lane[N]`, concise declarations, `connect`, `module M : Ifc` | [Composition](hierarchy-protocols.md), [named interfaces](named-module-interfaces.md) |
| Streaming | `rv<T>`, `credit<T,N>`, `request_response`; bounded `transform pipeline(auto)` | [Protocols](hierarchy-protocols.md#readyvalid), [elastic automatic pipelines](optimization-formal.md#automatic-pipelines) |
| Standard buses | `import std.bus.*` | [Aggregate protocols](hierarchy-protocols.md#aggregate-protocols-and-the-standard-library) |
| CDC | `sync_level`, `pulse_toggle`, `handshake`, `async_fifo` | [CDC](hierarchy-protocols.md#clock-domain-crossings) |
| Verification | named `assert`, `cover`, scoped `contract`/`require`/`ensure`; legacy `assume`/`guarantee` | [Verification UX](optimization-formal.md#first-class-verification-goals-and-contracts) |
| Implementation policy | `choice`, `pipeline(auto)`, `architecture(auto)`, `explore` | [Optimization](optimization-formal.md#one-implementation-policy-path) |

## Essential semantic rules

- Hardware assignments are concurrent. `=` drives a current-cycle value; `<-`
  schedules next-state.
- Assignment is exact. ZLang does not silently resize, perform a numeric
  signed/unsigned conversion, rescale fixed point, insert protocol adapters, or
  cross clock domains. The one documented representation-only exception is an
  equal-width raw boundary involving `bits<N>` or flat `vec<N,bit>`; it inserts
  the same `Bitcast` as the explicit spelling and never changes width or scale.
- `truncate(expr)` and `extend(expr)` are concise but still explicit
  conversions. They are accepted only when an exact typed boundary determines
  the result; inferred or nested expressions retain an explicit `<N>`.
- Unsuffixed literals use their minimum exact unsigned or signed type unless a
  direct literal appears at a concrete typed boundary. Context never changes
  the width of a compound expression or one operand of `concat`.
- Every runtime expression has a concrete hardware type after specialization.
  Generic functions and operator bodies are monomorphized into one retained
  typed callable definition per exact specialization; use sites are typed
  calls, not runtime polymorphism. Analyses needing concrete arithmetic use a
  bounded expansion service, while nominal reductions remain opaque to
  reassociation and the frozen e-graph rules.
- Additive reduction over a nominal struct uses its exact `operator +` overload
  at each node of one deterministic balanced source-order tree. The compiler
  does not synthesize component-wise field arithmetic or reassociate the tree.
- Unary numeric/nominal `-`, bit-only runtime `!`, and exact-width scalar
  bitwise complement `~` are implemented. Complement preserves the operand's
  hardware width and does not imply extension or signedness conversion.
  Non-nested `/* ... */` comments are implemented alongside `//` comments.
- Compile-time functions and `if` elaborate values/types and never become
  runtime real arithmetic or procedural control flow.
- Protocol observations such as `.transfer`, FIFO status, and request/response
  channel events have typed, read-only meanings. They are not arbitrary fields.
- Clash is the primary/general backend. Direct SystemVerilog is a stable,
  fail-closed supported secondary backend; see its exact
  [coverage matrix](direct-systemverilog.md).
- Both backends publish the same mandatory integration boundary: top-level
  struct fields and tuple `itemN` components are recursively named leaf ports,
  while `vec<N,T>` values are native unpacked arrays (one array per terminal
  field/component for vectors of structs or tuples).
  Packed aggregate values are private core/component details, never an
  alternate public ABI; see
  [backend tooling](backends-tooling.md#public-rtl-boundary).

## Validated real-design map

These designs are compiler/backend validation evidence, not promises that every
adjacent protocol or architecture is implemented:

| Design | What is validated | Deliberate boundary |
| --- | --- | --- |
| [SimpleDMA](dma-validation.md) | parameterized hierarchy, state, request/response, buffering, real Clash/direct-SV RTL and Verilator | no AXI, CDC, scatter-gather, or cross-module optimization |
| [Standard-library real designs](stdlib-and-real-design-validation.md) | AXI4-Lite/APB/Wishbone to RegBus/CSR, AXI-Stream packet flow, fixed FIR, and multi-channel DMA | the original QoR table is technology-independent evidence, not an Fmax claim |
| [FFT512 SDF reference](../examples/fft/README.md#fft512-nine-stage-functional-reference) | nine numerical stages, initialized twiddle ROMs, II=1/latency 520, and routine backend-independent plus dual-backend RTL replay against one frozen oracle | no automatic DSP mapping, physical QoR, or Fmax claim |
| [802.11a transmitter validation](80211a-transmitter-validation.md) | IEEE-authoritative bounded 6/12/24-Mbit/s framer through exact inverse DIF-SDF IFFT64, natural-order reorder, 80-sample CP, and complete direct-SV/Clash RTL packet replay | first functional dual-bank/single-buffered architecture misses the 10 ns physical constraint; no receiver, full rate set, or production certification claim |
| [IFFT64 numerical reference](80211a-transmitter-validation.md#ifft64-numerical-reference-elaboration-boundary) | full N=64 semantic/canonical/simulator result using compact functional IR; N=8/N=16 combinational backend witnesses | the whole-vector reference remains distinct from the production streaming SDF validated by the 802.11a project |

The exact current direct-SystemVerilog example count and tool versions live in
the [current status snapshot](current-language-status.md) and are explained by
the [backend matrix](direct-systemverilog.md#exhaustive-example-matrix), rather
than being treated as a language guarantee here.

## Supported standard-library namespace

`std` is a logical compiler-shipped namespace. For example,
`import std.bus.reg` resolves to ordinary source at `stdlib/bus/reg.zl`.
Qualified logical imports such as `import std.math.complex as cx` provide
source-local `cx.Complex`/`cx.function(...)`/`cx.Struct { ... }` references and
normalize to the same declaration identities as the unqualified spelling; see
[Projects and dependencies](projects-dependencies.md).
Available areas include fixed/complex math, stream/storage/coding/DSP helpers,
RegBus, AXI4-Lite, APB, AXI-Stream, Wishbone, and target/resource descriptions. The compiler core understands
generic types, protocols, hierarchy, and ownership; it does not encode bus
transaction names as privileged backend behavior.

## Detailed implementation guides

The topic guides and [current status](current-language-status.md) describe the
public product surface. Historical milestone reports and internal design-review
records are intentionally not part of the slim public distribution. Current
normative entry points include:

- [Types and numerics](types-and-numerics.md)
- [Expressions, functions, and generics](expressions-functions-generics.md)
- [Sequential state and storage](sequential-state-storage.md)
- [Hierarchy and protocols](hierarchy-protocols.md)
- [Direct-SystemVerilog support](direct-systemverilog.md)
- [Target-aware pipeline planner](high-level-target-aware-architecture-pipeline-planner.md)
- [Optimization and formal verification](optimization-formal.md)
- [Standard library](stdlib.md)
- [Syntax support matrix](syntax-support-matrix.md)
- [Known limitations](known-limitations.md)

## Current explicit boundaries

The current executable language intentionally does not claim:

- runtime packed-bit selection, runtime or reversed slicing,
  one-operand/heterogeneous-vector concatenation,
  runtime reshape, enum `bitcast`/`pack`/`unpack`,
  reloadable/asynchronous/multiport ROM, or external top enum inputs;
- runtime procedural `if`/`else`, mutable software locals, or general HLS;
- traits, runtime polymorphism, generic methods, implicit numeric conversions, or
  recursive generic programming;
- arbitrary or nested dynamic vector writes/ranges, runtime-selected instance
  inputs, and runtime-selected protocol endpoints; one range-proven element
  update to a one-dimensional `reg vec<N,T>` and read-only output projection
  such as `lane[select].value` are supported;
- asynchronous/multiport memories, writable-memory initialization, or automatic
  ROM/BRAM exploration;
- automatic protocol adaptation, implicit CDC, full AXI4 bursts/IDs, package
  registries, mutable dependency revisions, or unlocked external imports;
- liveness/eventuality/fairness, arbitrary temporal or solver-specific source
  properties, general hierarchical M36/M38, or new recursive formal observation
  families; named same-cycle `assert`/`ensure`, scoped environment `require`, and
  bounded `cover` are supported;
- automatic module substitution or backend selection by arbitrary IR region.

Named module interfaces are supported as exact conformance contracts; the
boundary above means that they do not themselves select or substitute an
implementation.

Unsupported forms must produce diagnostics or explicit backend skips; they must
not silently change hardware behavior.

## Syntax and editor conformance

The current executable language tour is
[`examples/all_syntax.zl`](../examples/all_syntax.zl). Separate registry
witnesses cover implemented forms not represented in that one file. The repository-owned VS
Code extension lives at
[`editors/vscode/zlang-vscode`](../editors/vscode/zlang-vscode). It highlights
keywords, declarations, built-ins, types, protocol properties, and operators
lexically. Always use `zlangc --check` for parser and semantic validation.
