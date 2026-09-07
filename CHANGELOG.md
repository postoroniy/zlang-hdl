# Changelog

All notable public changes to ZLang HDL will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
During the alpha series, source syntax, Python APIs, and serialized formats may
change incompatibly when the release notes identify the change. Versioned IR,
artifact, lock, manifest, and verification schemas continue to reject
incompatible input explicitly.

## [Unreleased]

Planned first release: `0.1.0a1`. It has not yet been tagged or published.

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

[Unreleased]: https://github.com/postoroniy/zlang-hdl/commits/main
