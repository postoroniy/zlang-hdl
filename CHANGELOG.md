# Changelog

All notable public changes to ZLang HDL will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
During the alpha series, source syntax, Python APIs, and serialized formats may
change incompatibly when the release notes identify the change. Versioned IR,
artifact, lock, manifest, and verification schemas continue to reject
incompatible input explicitly.

## Unreleased

## 0.1.0a9 — 2026-09-17

### Changed

- Consolidated the Community documentation into the maintained language
  reference, quick reference, and one reviewed 93-page PDF with the ZLang HDL
  cover on its title page. PDF build collateral remains outside the public
  projection.
- Reduced the VS Code package to a deterministic 13-file runtime bundle,
  required the current supported VS Code line, and exercised Definition and
  References through the installed VSIX host.
- Tightened the exact-public-tree, release metadata, reproducible-package,
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

## 0.1.0a5 — 2026-09-11

### Added

- Backend-independent scheduled value graphs for deterministic physical
  partitioning of supported pure scalar `pipeline(N)` expressions.
- Exact typed arithmetic e-graph alternatives and bounded Xilinx 7-Series
  DSP48E1 covering for multiply, add/sub, preadd and signed-product chains.
- Per-stage pipeline reports with stable operation identities, balancing
  delays, cost provenance and selected resource configuration.

### Changed

### Fixed

- Formal observation outputs can be added to a staged direct-SV datapath
  without invalidating its single scheduled output or changing production RTL.
- Unsigned DSP operand legality accounts for the required physical sign bit,
  and malformed same-stage dependency cycles are rejected deterministically.
- Generic Fmax is derived from the scheduled graph or remains unknown instead
  of using a fabricated constant estimate.

## 0.1.0a4 — 2026-09-10

### Added

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

## 0.1.0a3 — 2026-09-08

Community alpha with mathematical/formal examples and a downloadable lexical
editor package. Compiler semantics, license terms and the Community Baseline
remain unchanged; publication is subject to `RELEASING.md`.

### Added

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
