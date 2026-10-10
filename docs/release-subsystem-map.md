# Community release subsystem map

This map groups the prospective `v0.1.0a21` source candidate by review domain.
It is a navigation aid, not release evidence and not a substitute for the
regression ledger, exact-tree preflight, or hosted candidate workflow.

## Language semantics

- Explicit stored outputs use `out reg`; transient rule/FSM wire effects use
  `drive`. `<-` updates state only.
- CSR read/write events can select the existing `active_transfer` phase or the
  new explicit `post_accept` phase.
- Bounded structural generation accepts nested `generate` and compile-time
  `if` without adding runtime topology construction.

Review owners: parser rules, typed AST/IR, semantic state/action owners,
[`docs/language-reference.md`](language-reference.md), and focused parser,
semantic, native, and Direct-SV tests.

## Optimization and implementation intent

- Eligible `implement` roots retain a bounded set of certificate-checked exact
  value structures.
- Structural exploration is target-neutral and count-bounded. Session/workspace
  cache reuse is origin-free, bounded, and certificate-replayed.
- `maximize fmax` is evidence-ranked candidate selection, not an achieved
  device or silicon frequency guarantee.

Review owners: `zlang.intent_structural_exploration`, `zlang.opt.*`, generic
candidate exploration, target planning, implementation identities, and the
exploration/cache/Fmax regression suites.

## Temporal and formal

- The sole automatic II-greater-than-one candidate remains the explicit
  ready/valid integer `a*b+c*d` subset: one multiplier, latency 4, II 4,
  capacity 1, non-interleaved `RETIRE_AND_RELOAD` admission.
- Its transaction-stream route is bounded BMC evidence. `required_bmc` needs
  `bounded_pass`; `required_proven` remains unsupported and fail-closed.
- Solver-matrix qualification consists of separate executions over the same
  immutable bundle and compiler lowering. Agreement is corroboration, not an
  independent front-end proof or a stronger result status.

Review owners: temporal IR/admission/storage, shared arithmetic, formal route
and evidence/cache owners, verification bundle/report owners, randomized
native-versus-Verilator tests, and pinned formal jobs.

## Hierarchy and Direct SystemVerilog

- Child specialization identity no longer depends on parent physical context
  or aggregate-output consumption.
- Flattened public ABI collisions fail with both logical owners.
- Private FSM/helper names are readable and deterministic while semantic
  identities and public ABI remain separate.

Review owners: hierarchy specialization and ABI finalization, backend naming,
module-local emission owners, source maps/manifests, and repeated-emission
identity tests.

## Editor, tooling, and guidance

- Compiler-owned observations remain the only source of diagnostics,
  navigation, hover, completion, signature help, rename, and semantic tokens.
- The vendor-neutral AI guide, Qwen skill, and formal-verification guide point
  to the same executable language/support contracts.

Review owners: tooling session/query/navigation/diagnostic owners, LSP protocol
tests, installed VSIX host smoke, and documentation example validation.

## Release and security

- OSV Scanner findings exits are distinguished from infrastructure failure;
  exact inventory coverage and reviewed exceptions remain fail-closed.
- The locked VSIX dependency graph removes the affected transitive
  `brace-expansion` versions.
- Local and hosted release lanes use repository-owned Make targets, isolated
  checkout-local environments, two zero-skip deterministic suites, and exact
  protected-main preflight before publication.

Authoritative evidence remains:

- [`CHANGELOG.md`](../CHANGELOG.md) for curated shipped behavior;
- [`release/regressions.json`](../release/regressions.json) for durable fix
  identities and exact selectors;
- [`release/status.json`](../release/status.json) for the finalized release
  and tool/artifact identity;
- [`docs/release-process.md`](release-process.md) for candidate and publication
  gates.

## Explicit non-claims

This candidate does not claim general HLS, exhaustive optimization, arbitrary
II scheduling, timing closure, unbounded temporal proof, automatic CDC,
complete CDC sign-off, or general FPGA/ASIC memory-macro mapping. The public
repository topic `hls` should be removed because it overstates the current
bounded implementation-search and temporal-sharing surface.
