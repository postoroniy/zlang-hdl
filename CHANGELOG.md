# Changelog

All notable public changes to ZLang HDL will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
During the alpha series, source syntax, Python APIs, and serialized formats may
change incompatibly when the release notes identify the change. Versioned IR,
artifact, lock, manifest, and verification schemas continue to reject
incompatible input explicitly.

## Unreleased

## 0.1.0a21 — 2026-10-11

Community alpha adding explicit output-state ownership, bounded exact
implementation exploration, target-aware Fmax ranking, a narrow formally
checked capacity-one sharing route, and opt-in per-bit native logic-state
simulation. These facilities remain bounded by the documented alpha support
matrix and do not introduce general HLS, unbounded temporal proof, or full
electrical X/Z simulation.

### Added

- Native simulation JSONL schedules accept a bounded positive `repeat` count
  per event, allowing long idle, stall and backpressure intervals without a
  project-specific generator.
- Native simulation has an opt-in per-bit `0`/`1`/`U`/`X` logic-state mode.
  Uninitialized register bits begin as `U`, accepted writes turn unresolved
  `U` into computed `X`, controlling values resolve uncertainty where exact,
  and strict observation reports `U` and `X` distinctly. Initial-register
  overrides, logic-valued input events, selective VCD traces, and the companion
  `__zlang_meta/*_u_mask` signals preserve the otherwise unrepresentable `U`
  provenance. Initial overrides use compiler-owned hierarchical state paths;
  failed edges roll back logic and trace state atomically, and storage-plane
  synchronization writes only changed registers or memory cells. The ordinary
  two-state path remains unchanged; `Z` and
  electrical resolution are not modeled. The native plan/runtime contract
  advances to ABI v12.
- Formal bundle replay has an explicit immutable `smtbmc` execution-route
  identity and an opt-in deterministic solver matrix. Hosted nightly evidence
  requires separate Z3, Boolector and Bitwuzla executions and records Yices and
  cvc5 as corroborating executions over the same immutable bundle and compiler
  lowering; solver agreement never promotes bounded evidence to an unbounded
  proof or detects every shared front-end/lowering defect.
- Eligible `implement` sites perform bounded, deterministic exact-value
  exploration and retain structurally distinct alternatives for the existing
  candidate providers. Count-based hard ceilings and session-owned,
  certificate-rechecked caching bound the work without caching target, intent,
  or formal outcomes.
- The capacity-one ready/valid `a*b+c*d` implementation has an executable
  transaction-stream BMC route. `available` retains honest advisory evidence,
  `required_bmc` requires an actual `bounded_pass`, and `required_proven`
  remains fail-closed because no unbounded proof is claimed. Machine-readable
  evidence now publishes the exact bounded relation, depth, solver/tool
  context, reset contract when represented, latency, II, capacity and
  same-edge retire/reload facts; solver matrices distinguish a shared immutable
  problem from per-solver runs and report disagreements explicitly.
- A vendor-neutral AI guide, an updated Qwen project skill, and a dedicated
  formal-verification guide describe exact types/interfaces, evidence status,
  and the typed-IR-to-Direct-SV binding and solver flow.
- `out reg name : T [@clock] [= reset]` declares output-visible state. It
  follows ordinary register hold, clock-domain and optional-reset semantics;
  protocol endpoints remain explicit protocol ports rather than registered
  scalar outputs.
- `drive name = expression` is the explicit transient scalar-output action in
  rule, priority and FSM action blocks. An undriven cycle produces zero.
- Bounded structural generation accepts nested `generate` blocks and
  compile-time `if` branches while retaining the existing total expansion
  limit.
- CSR `on_read`/`on_write` events may select explicit `post_accept` timing.
  The selected write-data or read-hit event is captured at the accepting edge
  and published for the following cycle; existing events retain their
  `active_transfer` default. CSR JSON and Markdown maps record the phase.

### Changed

- Public examples now have one indexed ownership catalog. The IEEE 802.11a
  project consolidates related coding/scrambling and IFFT/cyclic-prefix source
  owners, uses manifest/lock-driven native simulation and formal commands, and
  retains generated artifacts and reduced reproducers under explicitly
  documented regression ownership.
- `zlang sim` is documented as the single native simulation command and no
  longer advertises a redundant engine selector. The previously accepted
  `--engine native` spelling remains a hidden migration no-op for this alpha;
  removed `reference`, `python`, and `jit` selectors still fail explicitly.
- `maximize fmax` reaches target planning unchanged. Under
  `measured_preferred`, exact routed evidence ranks before synthesis evidence
  and structural estimates; `measured_required` remains fail-closed when no
  matching measured evidence exists. This is evidence-ranked candidate
  selection, not an achieved device/silicon frequency guarantee.
- Verification run reports advance to schema v9 and the result cache to v4 so
  the exact formal execution route participates in run and cache identities;
  older records fail closed rather than guessing a route.
- `<-` now denotes state update only. Applying it to an ordinary `out` wire is
  a source error that points at the exact target and explains the explicit
  `out reg` and `drive` alternatives; no unsafe automatic quick fix is offered.
- Compiler-private FSM rule signals use readable transition-derived names.
  Semantic rule identities and public ports remain unchanged; the RTL naming
  schema advances to v5.

### Fixed

- Native vulnerability auditing distinguishes OSV Scanner's documented
  findings exit from infrastructure failure, parses complete reports before
  applying exact reviewed exceptions, and rejects inconsistent exit/report
  combinations, incomplete coverage, and stale exceptions.
- The locked VS Code build inventory updates transitive `brace-expansion` to
  5.0.12, closing the current nested-brace denial-of-service advisories before
  VSIX packaging.
- Child specialization identity no longer depends on physical parent context
  or aggregate-output consumption, so repeated aggregate children and sibling
  FSM modules retain one declaration/binding/content-owned specialization.
- Public aggregate ABI finalization rejects ambiguous flattened leaves such as
  `queue.head.valid` and `queue.head_valid`, reporting both logical paths and
  source spans instead of silently choosing one.

## 0.1.0a20 — 2026-10-07

Community alpha carrying bounded runtime packed selection, target-neutral
implementation-provider foundations, and a deliberately narrow ready/valid
temporal-sharing implementation. Existing scalar `implement` behavior,
arithmetic semantics, Direct-SystemVerilog ABI, and native simulation ABI v11
remain unchanged outside the documented new candidate forms.

### Added

- Runtime packed-bit indexing and fixed-width packed slices are accepted only
  after compiler range proof. Native simulation and Direct SystemVerilog use
  the same LSB-zero selection semantics and reject unproven accesses.
- `implement` now evaluates a bounded, target-neutral II=1 CSA multiplier
  alternative for exact uniform integer products up to 16 bits per operand.
  The existing generic product remains available and the candidate is checked
  through the normal semantic-reference formal path.
- The target candidate-provider boundary now has a `multiply` family. The
  source-described Series-7 `Xilinx7Multiply` templates can select an exact
  DSP48E1 full-width product when the existing explicit physical binding and
  legal latency configuration are present; unsupported shapes fail closed to
  generic logic.
- One explicitly flow-controlled ready/valid transform may select the exact
  integer kernel `a*b + c*d` with one shared multiplier. It is
  non-interleaved, has capacity one, latency four and II four, and supports
  same-edge output retirement plus next-input admission. Scalar/fixed-rate
  `implement` remains II=1 only; required-formal candidate selection stays
  fail-closed for this bounded BMC-only temporal relation.

### Changed

- Direct-SystemVerilog-only CLI emission demands only its required compilation
  products. Formal, report and document products remain demand-driven.
- Candidate providers, target capabilities, memory capability assessment, and
  temporal scheduling/resource-binding records now have target-neutral,
  compiler-owned boundaries. They do not introduce a general HLS scheduler.
- Development and CI commands use the checkout-local `.venv` and a unique
  `build/tmp` scratch directory; they reject sibling/ambient virtual
  environments rather than silently importing another checkout.

### Fixed

- Direct-SystemVerilog constant normalization preserves typed widths,
  signedness, origins and expression DAG sharing while folding exact constant
  subexpressions before rendering. This bounds redundant generated expression
  work without changing emitted hardware semantics.

## 0.1.0a19 — 2026-09-29

Community alpha carrying two compiler-owned CSR interoperability fixes and a
safer pre-tag release-candidate path. Language timing, numerical semantics,
generated RTL behavior outside the corrected CSR cases, and formal policy are
unchanged.

### Added

- Reusable CSR groups may contain native `split<32>` 64-bit values. Group
  expansion, child hierarchy projection, simulation and Direct SystemVerilog
  retain one authoritative typed logical value.
- The release workflow can run the exact release validation and EDA gates
  manually on a prospective tag without publishing. Tag-triggered publication
  remains conditional on the signed tag matching protected `main` exactly.
- A versioned release regression ledger binds every accepted fix to permanent
  source paths and focused tests. Release preflight now fails closed when a
  recorded fix, source, test selector, baseline tag or disposition is missing.

### Fixed

- CSR `ro` and sticky-W1C bindings can consume exact members of typed aggregate
  input ports. The compiler retains the member expression and rejects missing
  fields, type mismatches, command-member targets and implicit clock-domain
  crossings.

### Compatibility

- The separate native runtime wheel is version `0.1.0a19`; the serialized plan
  and runtime contract remain ABI v11.

## 0.1.0a18 — 2026-09-29

Corrective alpha carrying the accepted `0.1.0a17` compiler, native simulator,
editor and documentation content after its release EDA gate selected the wrong
Z3 executable. Language, compiler, generated RTL and verification semantics are
unchanged.

### Fixed

- The release EDA job now prepends the pinned Python scripts directory before
  checking the tool inventory, so Z3 4.13.4 is selected even when the OSS CAD
  Suite also provides Z3 4.15.5.

### Compatibility

- The separate native simulator wheel is version `0.1.0a18`. Its plan/runtime
  contract remains ABI v11 and is otherwise byte-equivalent in implementation
  to the reviewed `0.1.0a17` runtime source.

## 0.1.0a17 — 2026-09-28

This alpha carries the reviewed native simulator source and accepted compiler
fixes forward under a new version. Its plan/runtime contract is ABI v11; it
does not reuse the published a16 tag.

### Added

- Nightly Z3-backed standard-library contracts and a source- and JUnit-bound
  coverage inventory distinguish proven properties, bounded checks, partial
  coverage, and unverified declarations. These checks do not claim complete
  AXI4 or FIFO verification.
- `zlang sim --compare-with iverilog|verilator` emits the normal direct
  SystemVerilog implementation, drives the same event schedule, and compares
  every physical output after every event with the native simulator.

### Fixed

- The 802.11a eager-result regression now compares physical input content
  without depending on checkout path ordering or duplicate installed stdlib
  copies. Compiler semantics and generated RTL are unchanged.
- Community example inventory no longer silently excludes an 802.11ad
  experiment; the public source tree is explicitly checked to omit it.
- Formal release checks use Z3 4.13.4 consistently with the executable
  standard-library contract tests.

### Validated

- The native simulator and compiler complete bounded checks of the large
  structural receive-shim source used in private regression review; the source
  and its tests are not part of the Community release projection.

### Compatibility

- The separate native runtime wheel is version `0.1.0a17`. Its serialized plan
  and runtime ABI advance to version 11; mixing older compiler/runtime packages
  fails closed.
- Native is the only simulation engine. The retired `reference`, `python`, and
  `jit` selectors produce a migration diagnostic; independently generated RTL
  can be checked with either Icarus Verilog or Verilator.

## 0.1.0a16 — 2026-09-25

Community alpha focused on bounded compilation and simulation of large
structural designs, plus LSP resilience. Language, fixed-point, RTL and formal
semantics are unchanged.

### Fixed

- Shared typed expression subgraphs remain shared when pure functions are
  instantiated, preventing exponential compiler and editor memory growth.
- LSP requests have bounded supervision so an unexpectedly expensive analysis
  cannot leave the editor waiting indefinitely; ordinary diagnostics and
  navigation retain compiler-owned semantics.
- Reference and native simulators now lower nested pure functional regions
  into bounded primitive-plan regions without duplicating their bodies. A large
  structural stress case now runs with exact reference/native/typed cycle
  parity rather than exhausting the plan budget.

### Compatibility

- The serialized native simulation plan and runtime ABI advance to version 10.
  The Python compiler and separate Linux x86-64/WSL2 native wheel must both be
  version `0.1.0a16`; mismatched older wheels fail closed. The independent
  reference simulation engine remains available.

## 0.1.0a15 — 2026-09-24

Corrective Community alpha carrying the accepted `0.1.0a14` compiler tree after
its unpublished release workflow exposed an over-broad persistent-cache test.
It retains that candidate's native simulator, LSP live editing, AXI4 stdlib and
release-hardening changes without altering compiler or hardware semantics.

### Fixed

- The LSP persistent-symbol-cache regression now inspects only its owned
  `symbol-v1` namespace instead of also counting incremental workspace parse
  cache shards beneath the shared XDG cache root. Product behavior is unchanged.

## 0.1.0a14 — 2026-09-23

Community alpha consolidating accepted compiler, simulation, LSP live editing and
standard-library work since `v0.1.0a13`. The native simulator remains a separate
audited Linux x86-64/WSL2 binary; its Rust/Cranelift sources and all private
development collateral are excluded from the Community source tree.

### Changed

- Full AXI4 source types, bounded managers/subordinates, exclusive-access
  helpers and flat pin adapters are now included in the standard library.
- Incremental workspace reuse avoids repeated semantic and native compilation
  when exact or parser-proven trivia-only editor snapshots can be rebound safely.
- Common serialization, identity, graph traversal and compiler-session products
  have single typed owners, reducing duplicated Python compiler work without
  changing language or hardware semantics.
- Native and reference simulation handle wide packed values and shared DAGs
  with explicit complexity bounds; hierarchy and protocol execution continue to
  lower to the generic primitive simulation plan rather than Rust-side ZLang
  semantics.
- Future release workflows independently scan the exact CycloneDX inventory
  embedded in the binary native-simulation wheel and retain fail-closed advisory
  evidence alongside the existing structural, license, and dependency audits.
- GitHub releases again include the exact reviewed language-reference PDF from
  the tagged source tree, covered by release checksums and artifact attestation;
  the workflow never rebuilds the PDF.
- Release acceptance now stops each regression or performance pass after its
  first failure, and does not start later passes or publication after a failed
  gate.
- The reviewed VS Code packaging tool is updated to `@vscode/vsce 4.0.0`; its
  exact direct dependency and lockfile remain enforced by the editor audit.

### Fixed

- LSP live-edit snapshots no longer report dirty locked modules or request
  failures while related open project files are being edited, and current spans
  are retained for diagnostics and navigation.
- Child protocol member projection and aggregate protocol simulation now use
  the compiler-owned typed composition model consistently across reference,
  native and Direct-SV execution.

## 0.1.0a13 — 2026-09-23

Corrective release of the unchanged `0.1.0a12` compiler, native simulator,
LSP live editing and documentation payload. The persistent LSP symbol-cache
regression now inspects only the symbol-cache namespace, so the legitimate
incremental workspace parse cache cannot make repeated hosted release runs
order-dependent. Release and EDA validation remain on GitHub-hosted Ubuntu
runners.

## 0.1.0a12 — 2026-09-23

Corrective release of the unchanged `0.1.0a11` compiler, native simulator,
LSP live editing and documentation payload. Runtime scalability checks now run
in the dedicated serial performance gate instead of competing under the
parallel correctness regression. The pinned CodeQL and open-EDA setup actions
are updated to their reviewed Dependabot revisions. All release acceptance
remains on GitHub-hosted Ubuntu runners.

## 0.1.0a11 — 2026-09-22

Community alpha with a persistent native simulator, more reliable live editing,
and compiler scalability/correctness improvements. The Python reference
simulator and direct-SystemVerilog backend remain independent execution paths.

### Added

- `zlang sim` and the `zlang.sim` API support persistent simulation, batched
  events and VCD traces. The native Cranelift executor is distributed in an
  audited Linux x86-64 binary wheel (also usable on WSL2). macOS native wheels
  are deferred. The Community source tree and Python archives contain no
  Rust/Cranelift implementation; `--engine reference` remains available without
  the binary wheel.
- A structural synthesis witness suite covers 18 independent hardware patterns,
  including reductions, permutes, crossbars, compactors, CAM, FIR and CRC.
- A source-level `std.bus.axi4` preview adds five-channel types, bounded
  manager/subordinate controllers and flat pin adapters. It is not yet a
  validated drop-in AXI4 endpoint; `AXI4Lite` and `AXI4BurstSubset` retain
  their existing interfaces.

### Changed

- LSP live editing uses project-wide open-document snapshots and debounced
  diagnostics. Unsaved imported modules no longer surface stale-snapshot errors
  as completion or navigation failures.
- Exact callable simplification, semantic expression sharing, nested functional
  regions and Direct-SV DAG materialization reduce accidental elaboration and
  duplicated RTL without changing source-level hardware semantics.
- The `zlang` command now exposes simulation, verification, project locking and
  LSP subcommands while retaining the corresponding compatibility executables.
- The public installation and language documentation remains consolidated into
  the maintained reference, quick reference and one reviewed PDF. The released
  compiler is direct-SystemVerilog-only; private development and PDF-build
  collateral are not part of the Community snapshot.
- Mandatory public regression and open-EDA release gates now run on
  GitHub-hosted Linux runners. Fixed-seed PR cases and twice-daily full plus
  reproducible random regression retain failing sources, RTL and logs as
  Actions artifacts; no self-hosted runner is required.

### Fixed

- CSR hierarchy and emitter cases, typed immutable child bindings, and
  preserved memory read-data reset behavior found during ZTPU migration.
- CLI diagnostics retain source file, line, column and error category across
  multi-module project compilation.

## 0.1.0a10 — 2026-09-17

Documentation and release-governance correction with no compiler semantic or
RTL behavior change.

### Changed

- Corrected the Community baseline to identify published `v0.1.0a9`, name
  direct SystemVerilog as the sole production backend, and record Clash only as
  a retired pre-baseline experiment.
- Removed internal source-projection, release-checklist and future-product
  collateral from the Community repository while retaining the concise
  language references and reviewed PDF.
- Made future GitHub releases use the exact matching `CHANGELOG.md` section as
  curated release notes instead of an automatically generated pull-request
  list.

## 0.1.0a9 — 2026-09-17

Community alpha focused on a consolidated language reference, a lean current
VS Code/LSP distribution, and stronger reproducible release evidence. Compiler
language and RTL semantics are unchanged from `0.1.0a8`.

### Changed

- Consolidated the Community documentation into the maintained language
  reference, quick reference, and one reviewed PDF with the ZLang HDL cover on
  its title page. PDF build collateral is not included in the repository.
- Reduced the VS Code package to a deterministic runtime bundle, required the
  current supported VS Code line, and exercised Definition and References
  through the installed VSIX host.
- Tightened the source-tree, release metadata, reproducible-package,
  dependency, and hosted editor gates used to publish Community artifacts.

## 0.1.0a8 — 2026-09-16

### Changed

- Parser failures now retain a structured source span, so VS Code underlines
  the actual failing line instead of placing an otherwise correct diagnostic at
  the beginning of the file.
- Refreshed the verified open-source EDA matrix to Verilator 5.052, Yosys and
  SymbiYosys 0.69, and Icarus Verilog/VVP 14.0. Memory reset iterators are now
  block-local SystemVerilog loop variables, formal-aware selection freezes one tool-version
  snapshot per compiler configuration, and strict reset-synchronizer tests use
  one documented Verilator warning waiver without changing generated RTL.
- Consolidated LSP request/document validation, verification-bundle field
  decoding and safety verification/semantic-reference equivalence counterexample serialization behind single typed
  helpers. The independent numerical test oracles remain separate by design.
- Formal and optimization concepts now use descriptive names such as safety
  verification, semantic-reference equivalence and formal-aware selection in
  documentation, diagnostics, reports, cache namespaces and public test names.
  Older identity-bearing cache records fail closed under the renamed namespaces.
- Added non-publishing Makefile gates for static checks, focused/full tests,
  two-pass zero-skip release regression, exact-public-tree audits, pinned EDA
  inventory, editor tests and reproducible packaging. Packages are built from
  a fresh allow-listed Community export, preventing stale checkout `build/`
  files or private sources from entering wheel/sdist artifacts.
- **Breaking packed-ABI change:** indexed aggregates are now canonical
  LSB-first. `vec<N,T>[0]`, tuple `item0`, and `string<N>[0]` occupy the
  least-significant packed component, recursively; consequently
  `pack("AB") == 0x4241`. Raw bit numbering, `concat(high, low)`, first-declared
  struct fields at the MSB, and tagged-union tags at the MSB are unchanged.
  Manifests, physical/proof identities, simulation-state catalogs and cached
  artifacts from the previous packing schema are rejected rather than reused.
- Packaged AMD 7-Series FIR and signed-product DSP48E1 QoR was rerouted with
  Vivado 2024.2 against the new physical identities. The evidence generator now
  emits exact graph-keyed planner records, and `measured_required` again selects
  the verified DSP candidates without borrowing pre-migration measurements.

## 0.1.0a7 — 2026-09-15

### Added

- A complete installation guide for source and wheel installs, VS Code LSP
  wiring, OSS CAD Suite or separate Verilator/Yosys/SBY/Z3 setup, exact
  release-tested versions, and a real formal smoke test.
- First-class clock-domain ownership for registers, rules, concise FSMs,
  exact scalar pipelines, FIFO/memory/ROM resources, CSR state and compatible
  hierarchical children. Multi-clock modules now emit independent direct-SV
  sequential processes and one reset conditioner per physical domain.
- Domain provenance through dynamic combinational expressions, with explicit
  `sync_level`, `pulse_toggle`, `handshake` and `async_fifo` crossings as the
  only supported way to change provenance.
- Named same-clock read/write memory ports, bounded multiport physical planning,
  and explicit independent-clock `async_mem` 1W1R with domain-owned read state.
  Uniform compile-time `init VALUE` preserves generic reset and FPGA power-up
  initialization semantics without claiming analog collision guarantees.
- Community `zlang-lsp` and the independently packaged VS Code language client,
  with compiler-owned Definition/References, content-bound symbol shards and
  real installed-editor navigation acceptance.
- A tracked Community-only Qwen project skill with concise routing for `.zhl`
  authoring, compiler work, direct-SystemVerilog integration, optimization,
  formal verification, and standards-based conversion. The public projection
  requires the skill and its references while keeping it independent from the
  compiler and editor packages.

### Changed

- Consolidated duplicated source-origin codecs, signed DSP width calculation,
  constant-term extraction and semantic-reference equivalence expression traversal into shared compiler
  utilities without changing their serialized or arithmetic semantics.
- Scheduled-value and physical candidate identities now include resolved clock
  ownership. Existing packaged DSP48 QoR records were deterministically re-keyed
  to that identity schema; their measured Vivado values are unchanged.
- Canonical IR identity schema v14 records the resolved domain of child-output
  values, preventing hierarchy or inferred locals from erasing CDC provenance.
- Refreshed current-facing language, storage, standard-library, FFT, backend,
  and formal guides for the direct-SystemVerilog-only production policy, the
  current example-corpus manifest, and the shared e-graph/scheduler/resource
  responsibility split.
- Definition and References now share bounded multi-module top selection.
  References validates project-root bytes and candidate bounds instead of
  publishing stale or truncated sets; same-file enum and resource navigation
  retain exact compiler-owned identifier spans.

## 0.1.0a6 — 2026-09-11

### Changed

- Removed the retired Clash emitter, its hidden compatibility CLI, generated
  Haskell artifacts, packaging surface, test suite, and tool discovery. Direct
  SystemVerilog is now the only production RTL backend in both policy and code.
- Retired executable cross-backend comparison without reusing its name for
  another relation. Safety verification, direct-SV semantic-reference
  equivalence, and formal-aware selection remain supported.
- Release acceptance now requires zero skipped tests and no GHC/Clash tooling.

## 0.1.0a5 — 2026-09-11

### Added

- Backend-independent scheduled value graphs for deterministic physical
  partitioning of supported pure scalar `pipeline(N)` expressions.
- Exact typed arithmetic e-graph alternatives and bounded Xilinx 7-Series
  DSP48E1 covering for multiply, add/sub, preadd and signed-product chains.
- Per-stage pipeline reports with stable operation identities, balancing
  delays, cost provenance and selected resource configuration.

### Changed

- Direct SystemVerilog is the sole production RTL backend. The public Clash
  output options and `zlang-compare-backends` command are retired; the legacy
  emitter remains internal compatibility code only.
- formal-aware selection and compiler-owned selected-candidate equivalence now use the direct-SV
  semantic-reference equivalence route. retired cross-backend equivalence is retained only as unavailable historical schema data.
- Exact `pipeline(N)` scheduling is deferred until target/profile planning,
  preserving semantic latency while allowing real internal register cuts.

### Fixed

- Formal observation outputs can be added to a staged direct-SV datapath
  without invalidating its single scheduled output or changing production RTL.
- Unsigned DSP operand legality accounts for the required physical sign bit,
  and malformed same-stage dependency cycles are rejected deterministically.
- Generic Fmax is derived from the scheduled graph or remains unknown instead
  of using a fabricated constant estimate.

## 0.1.0a4 — 2026-09-10

### Added

- A concise language quick reference for coding agents and experienced users,
  linked from the full guide and checked against the current compiler surface.
- Shared compiler utilities for deterministic subprocess execution, backend
  binding identities, pipeline constraints and generated Clash signal logic.

### Changed

- `implement { ... intent { ... } }` is now the single scalar automatic
  implementation-policy form. Exact `pipeline(N)`, explicit `choice`, and the
  ready/valid `transform pipeline(auto)` form remain supported.
- Retired scalar `pipeline(auto)`, `architecture(auto)`, and `explore` spellings
  now produce deterministic migration diagnostics instead of maintaining
  parallel policy paths.
- Consolidated the former pipeline, architecture, and combined-exploration
  examples into one runnable implementation-intent source while preserving
  their distinct typed designs and candidate sets.

### Fixed

- Candidate discovery and formal evidence now follow the exact selected
  implementation identity, including bounded diagnostics for impossible
  resource policies and source-independent evidence identities.
- Clash catalog-only alternatives can no longer leak into selected generated
  RTL, and explicit positive latency intent consistently gates pipeline
  candidate generation.

## 0.1.0a3 — 2026-09-08

Community alpha with mathematical/formal examples and a downloadable lexical
editor package. Compiler semantics, license terms and the Community Baseline
remain unchanged; publication is subject to `RELEASING.md`.

### Added

- A reproducible eight-product datapath tutorial comparing one-cycle, balanced
  architecture and four-stage `explore` implementations. Recorded Vivado
  out-of-context timing uses the same device and 10 ns constraint; it is not a
  board-level timing guarantee.
- Separate semantic-reference and retired cross-backend bounded checks for the
  pipelined example, plus deliberate arithmetic and latency mutations. BMC
  remains bounded evidence; the separately timed-out architecture route
  remains explicitly `unknown`.
- A static VS Code extension for `.zhl`, independently versioned 0.1.0, with
  compiler-checked snippets, real TextMate/Oniguruma tests and three optional
  highlighting styles. No LSP, compiler runtime or telemetry is included.
- Audited VSIX and audit JSON assets in GitHub releases, covered by checksums
  and exact-tag workflow attestation. Marketplace/Open VSX publication is not
  part of this release.

### Fixed

- Refresh the immutable public-tree manifest for the pinned `setup-node` v7
  Dependabot update, preserving the selected Node.js 22.23.2 toolchain.
- Gate editor release packaging on a fresh advisory audit of all locked npm
  build dependencies as well as static package/license validation.

## 0.1.0a2 — 2026-09-08

Corrective Community alpha release cut; publication remains subject to every
gate in `RELEASING.md`. All compiler capabilities and fixes below are retained.

### Fixed

- Pin the patched installer used by release builds and fresh package installs.
- Audit the exact published dependency inventory, including installer tooling,
  before generating attestations or uploading release artifacts. Findings,
  skipped dependencies and incomplete/malformed reports fail the release gate.
- The preceding signed `v0.1.0a1` attempt was cancelled before GitHub Release
  publication because its inventory included vulnerable installer tooling.
  Its signed tag is retained unchanged; no compiler/runtime vulnerability was
  found by that audit.

## 0.1.0a1 — 2026-09-08 (release attempt cancelled)

First Community alpha release cut. The signed tag exists, but the GitHub
Release was not published; the installer inventory gate is corrected in a2.

### Added

- Initial experimental alpha of the ZLang HDL compiler.
- Public release, security, contribution, support, and provenance policies.
- The 2026-09 Community Baseline retains every included compiler capability.
  Future CSR C/C++ and UVM helper generators are classified Enterprise, not
  implemented additions; existing CSR and simulation exports remain Community.
- Typed semantic and canonical IR, simulation, Clash and direct-SystemVerilog
  backends, compiler-owned standard library, project locking, manifests, and
  bounded optimization and verification workflows.
- Real-design validation including DMA, standard-bus CSR paths, fixed-point FIR,
  FFT512, and an attributed IEEE 802.11a transmitter project.
- Four runnable formal examples and a tutorial covering invariant proofs,
  a deliberately seeded rare-input counterexample, scoped assumptions,
  bounded ready/valid safety, covers and immutable verification-bundle replay.
- Source-authored bounded AXI burst reader/writer helpers, scalable replicated
  two-read/one-write banked storage composition, and simulation-only state
  preload/inspection by stable semantic identity (ZL-003, ZL-005, and ZL-006).
- Source-authored, full-width AHB-Lite-to-RegBus support with the standard
  pipelined address/data relationship, two-cycle ERROR responses, and an
  active-low asynchronous-assert/synchronized-release reset contract.

### Changed

- Byte-masked writable memories now accept arbitrary positive packed widths;
  the mask has `ceil(width/8)` lanes and a partial most-significant lane affects
  only live bits.
- Writable memories can explicitly select combinational or registered reads and
  independently preserve or clear contents/read results on reset (ZL-002).
- Private RTL names use collision-safe hierarchy-local identifiers with single
  underscore separators; public top/port ABI and semantic identities stay intact.
- Callable representative selection and lazy parser construction reduce measured
  Wi-Fi compilation and CLI startup overhead without changing generated RTL.

### Fixed

- Preserve statically proved unsigned ranges through registered aggregate field
  projections used for runtime vector reads and element updates (ZL-001).
- Preserve typed signed right-shift and ordered-comparison behavior in direct
  SystemVerilog, the semantic-reference renderer, and formal predicates
  (ZL-012 and ZL-014).
- Deduplicate shared child specializations by their reachable callable closure,
  independent of unrelated parent-visible functions (ZL-015).
- Support recursive `when`/`else when`/`else` inside one atomic action group,
  including storage readiness, output conflicts, and scheduler/formal behavior
  (ZL-016).
- Preserve rule/state emission in standalone and composed direct-SystemVerilog
  dispatch, with emission-local hierarchy reuse (ZL-017).
- Use the actual effective reset for formal-only rule-fire observations;
  preserve recursive wrapper locators and avoid packed/leaf naming collisions.
- Limit manually dispatched dedicated EDA jobs to this repository's trusted main.
- Validate the signed tag and its main-branch target before release jobs use
  the dedicated self-hosted EDA runner.
- Isolate CLI diagnostic test inputs from the source tree so parallel release
  checks cannot mistake temporary inputs for missing published files.
