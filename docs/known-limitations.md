# Known limitations

ZLang `0.1.0a5` is an experimental alpha release.  The compiler deliberately
fails closed when a design falls outside a validated language/backend
intersection: it must not publish RTL after silently dropping an IR entity.

## Supported platform

- The release-supported host is Linux/POSIX x86-64 with Python 3.12.
- Direct SystemVerilog is the sole stable supported production backend for the
  repository's validated corpus.  Passing that corpus is not a claim that every
  future combination of otherwise supported features is accepted.
- Clash 1.11 is a hidden legacy compatibility emitter only; it is not required
  to install, compile, verify, or release normal designs.
- Verilator, Yosys, SymbiYosys, yosys-smtbmc, Z3, and vendor synthesis tools are
  external programs and are not installed by the Python package.

## Language and backend boundaries

- ZLang has no runtime procedural `if`, mutable local variables, implicit
  numeric casts, implicit fixed-point rescaling, or general HLS scheduler.
- Runtime-selected instance inputs/protocols, general cross-module atomic
  scheduling, full AXI4, automatic CDC insertion, and arbitrary stateful
  elastic pipelines are outside the alpha contract.
- The direct-SV production intersection is authoritative.  Hidden legacy Clash
  compatibility may support a different bounded intersection, but unsupported
  combinations must produce a structured diagnostic rather than partial RTL.
- The Python API is provisional.  The command-line interface and versioned
  artifact/lock/bundle schemas are the intended integration surfaces.
- Simulation-only architectural state access is currently a generic direct-SV/
  Verilator facility for one exact clock/reset domain. It does not expose
  backend-created FIFO, CSR, protocol, CDC, target-mapped, or Clash state and
  must not be confused with synthesizable memory initialization.

## Verification boundaries

- Bounded model checking is reported as `bounded_pass`, never as an unbounded
  proof.
- M35 safety, M36 semantic-reference equivalence, and M39 formal-aware
  selection run only when the required observations,
  domains, reset contracts, artifacts, and external tools are connected.
- M38 is retired with the production Clash backend. Historical M38 records
  remain audit evidence, not a current execution route.
- Unsupported routes are explicit `unknown` or `skipped`; they are not silently
  treated as success.
- General liveness/fairness, arbitrary temporal syntax, proof-fed optimization,
  and unrestricted hierarchical equivalence are not part of this release.

The machine-readable capability registry and
[syntax support matrix](syntax-support-matrix.md) are the detailed authorities
for individual constructs.  Report any accepted design that emits invalid RTL
as a correctness defect.
