# Changelog

All notable public changes to ZLang HDL will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
During the alpha series, source syntax, Python APIs, and serialized formats may
change incompatibly when the release notes identify the change. Versioned IR,
artifact, lock, manifest, and verification schemas continue to reject
incompatible input explicitly.

## Unreleased

## 0.1.0a12 — 2026-09-23

Corrective release of the unchanged `0.1.0a11` compiler, native simulator,
LSP live editing and documentation payload. Runtime scalability checks now run
in the dedicated serial performance gate instead of competing under the
parallel correctness regression. The pinned CodeQL and open-EDA setup actions
are updated to their reviewed Dependabot revisions. All release acceptance
remains on GitHub-hosted
Ubuntu runners.

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
- Retired executable retired cross-backend equivalence cross-backend comparison without reusing its name for
  another relation. safety verification safety, direct-SV semantic-reference equivalence semantic-reference equivalence,
  and formal-aware selection formal-aware selection remain supported.
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
- Independent semantic-reference equivalence semantic-reference and retired cross-backend equivalence cross-backend bounded checks for
  the pipelined example, plus deliberate arithmetic and latency mutations.
  BMC remains bounded evidence; the separately timed-out formal-aware selection architecture route
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
