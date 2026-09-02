# Whole-build manifests and evidence reports

ZLang can describe one reproducible build as a deterministic, versioned record.
The whole-build manifest joins the compiler stages, selected implementation
policy, backend products, companion files, actual tool executions, generated
reports, and typed evidence without making any of those records the source of
language semantics.

The first schema is `zlang-whole-build-manifest-v1`. It is complementary to,
not a replacement for, the per-backend `BackendArtifact` manifest.

## Compiler and implementation identities

A build records two distinct canonical compiler identities:

- `high-level:<sha256>` identifies the fully typed, backend-independent
  high-level canonical IR;
- `selected:<sha256>` identifies the selected canonical architecture IR.

The identities use the versioned canonical-identity schema and exclude source
spans and other attribution-only metadata. An optional canonical content hash
may accompany either reference. The high-level and selected identities are not
backend artifacts, RTL hashes, or synthesis-plan identities.

Each backend is planned independently from the same selected-IR identity. Its
record keeps these identities separate:

- the normalized backend plan identity;
- the backend `build_identity`;
- the exact emitted artifact hash and BackendArtifact version;
- an optional physical implementation-graph identity;
- an optional source-map hash.

This distinction prevents a physical direct-SystemVerilog resource plan from
being attributed to Clash, and prevents generated RTL from being mistaken for
the selected semantic design. Backend states are explicit: `selected`,
`generic_fallback`, `unsupported`, `failed`, or `not_requested`. A selected or
fallback build must publish an artifact, its manifest version, and at least one
content-addressed output. Unsupported, failed, and unrequested plans cannot
silently publish successful products.

## Published files and companions

The manifest records the exact compiled root-source bytes, the complete locked
project and compiler-shipped `std.*` dependency closure, backend outputs, and
backend companion bundles as `PublishedFile` values. Every value
contains:

- a normalized relative POSIX path;
- a SHA-256 content hash;
- a file kind;
- the exact byte size for build products;
- optional ZLang source attribution.

Absolute paths, `..`, non-normal paths, and duplicate publication paths are
rejected. Directory-root validation additionally rejects symlink escapes. The
CLI's explicit logical-path-to-physical-`Path` map checks every build product's
type, size, and content hash before publishing the manifest. Locked dependency
records are the exception: the project workspace has already validated their
content hashes before semantic analysis, so their physical cache/check-out
paths are intentionally absent from the public manifest validation map.

Before any product is written, the CLI also checks a private, non-semantic map
of every physical compiler input: the root snapshot, project manifest, lock,
root and dependency modules, dependency manifests, and every consulted stdlib
candidate. Explicit file sinks, generated companions, and compiler-owned output
or cache directories may not alias or contain any of those inputs. File sinks
may not alias one another, and owned directories may not overlap. The sole
compatibility exception is `--systemverilog` plus
`--experimental-systemverilog` naming the same file, because both flags request
the same bytes. Physical paths protect the workspace from overwrite; they never
enter semantic, artifact, cache, or whole-build identity.

Companions remain associated with the backend that needs them. For example, an
initialized ROM image is not hidden inside the RTL identity: the image is a
separate content-addressed companion and can be validated beside direct-SV
`$readmemb` output or in the Clash compilation workspace.

The CLI acquires the root source once as raw UTF-8 bytes. Semantic compilation
and manifest hashing use that same snapshot; CRLF is preserved, and a physical
source change before final publication rejects the manifest. Clash likewise
generates RTL in a fresh staging directory, so pre-existing Verilog below the
requested output directory is neither claimed nor hashed as a current result.
For a `--verilog-dir`-only build, the exact generated Clash source is retained
as a backend product so the `BackendArtifact` text hash remains verifiable.
Generated RTL and ROM companions are then published through one relative-path
publisher: every directory and leaf is opened without following symlinks, the
payload is fsynced in a same-directory exclusive temporary, renamed atomically,
and revalidated by content. Symlinked or non-regular destinations fail without
writing through them. Because Clash emits relative `$readmemb` references, the
same content-addressed image is recorded at the output root and beside each
generated Verilog module directory; all copies retain one semantic companion
identity and are hash-validated independently.

Generated source maps and rendered reports are content-validated in the same
way. A report's stable semantic identity is derived from its report ID, kind,
and ordered evidence IDs. Its rendering format, content hash, and optional
publication path remain serialized and tamper-checked, but intentionally do
not alter the whole-build identity. A report cannot refer to unknown evidence,
and a published report path cannot carry different bytes.

## Exact evidence meanings

Evidence is adapted only from typed compiler/formal result objects. Log strings
and ad-hoc result dictionaries are not evidence inputs. The stable statuses
mean:

| Status | Exact meaning |
| --- | --- |
| `typed_legal` | Semantic analysis produced a well-typed module. This is not a timing or proof result. |
| `timing_validated` | Every public scalar output was checked against the exact typed module timing contract. This is not a proof result. |
| `bounded_pass` | BMC found no counterexample through the recorded positive `depth`. It requires mode `bmc` and is never described as unbounded proof. |
| `proven` | An unbounded `prove` execution succeeded. Only this status denotes an unbounded proof. |
| `failed` | The executed check found a failure; typed counterexample attribution may be attached. |
| `unknown` | Execution completed without a pass, proof, or counterexample classification. |
| `skipped` | The route was explicitly unavailable or inapplicable. It is not success. |
| `not_run` | No proof execution occurred. It is not success. |

`bounded_pass` must include BMC mode and depth. `proven` must include prove
mode. Semantic and timing validation cannot carry proof mode/depth. An
unexecuted record is rejected if its claim says that something was proved.

The typed adapters preserve the applicable property ID, candidate identity,
backend and artifact hash, reference hash, engine, solver, relation, proof route,
mode, and depth:

- M35 records safety-property execution or an explicit unexecuted property;
- M36 records selected architecture versus semantic-reference equivalence;
- M38 records an executed Clash/direct-SV artifact pair;
- M39 records formal candidate eligibility without promoting an unexecuted
  route into proof evidence.

Positive M39 evidence requires a connected backend and artifact. The report
layer does not infer execution from cache metadata, registered rewrite rules,
generated properties, an SBY file, or a solver executable being installed.

## Harness generation is not proof execution

`--formal-harness` and `--formal-sby` generate formal inputs. They do not run a
solver and therefore cannot produce `bounded_pass` or `proven`. When no typed
execution result exists, the corresponding property is reported as `not_run`
and may explain why execution was not performed, but it must not imply success.
Artifact generation itself belongs in the backend/publication records, not in a
second evidence status.

The first-class verification UX adds an immutable bundle boundary rather than
changing that rule. `--verification-bundle` publishes hash-validated structured
verification IR, implementation/source-map inputs, and separate safety/cover
jobs. Exact ROM images are immutable `companion` inputs to every job that uses
them. `zlang-verify` or `zlang --verify` creates a run report only after real
execution. Solver, engine, depth, timeout, logs, tool versions, witnesses, and
counterexamples are run data and are not folded into the bundle's source
identity. A bounded cover miss is `bounded_unreached`, never proof of
unreachability.

Bundle schema v4 and verification-IR snapshot v3 describe the immutable input.
Raw run-report schema v7 separately records `run_identity`, exact execution
configuration, retained bounded-stage results, tool versions, and a work
directory for each safety/cover job. Generated SBY configuration,
stdout/stderr, timeout diagnostics, and VCDs live outside the bundle below
`--work-dir`/`--verification-work-dir`. A retained VCD may be decoded through
the immutable binding table into semantic signal/value pairs; paths and raw logs
remain outside `run_identity`.

A joint `--verify` plus non-`off` formal-policy run publishes
`zlang-compiler-verification-report-v1`. That wrapper links the raw v7 report to
the exact compiler execution plan and separately typed selected-candidate
M36/M38 reports. Candidate reports retain deterministic per-route work roots and
the discovered tool snapshot when execution needed tool discovery; exact
in-session M39 reuse also carries its recorded work root. Physical paths remain
operational metadata outside every semantic, run, evidence, and cache identity.
Persistent M39 cache payloads omit them rather than claiming that an old
workspace is still available.

Each safety/cover job is tied to one exact clock/reset pair. Multiple supported
synchronous domains may therefore appear as independent jobs in one bundle;
an unsupported domain skips only goals that name it. This is not a cross-domain
equivalence or temporal proof relation.

A requested prove run is staged behind BMC. Proof starts only when every safety
job is `bounded_pass`. Covers run once during BMC and are not rerun; an unrelated
cover result does not block safety proof, while an unwitnessed feasibility cover
first makes its dependent safety result vacuous/unknown and therefore blocks
proof. The merged report retains both the bounded cover evidence and any proved
safety results. This sequencing is part of execution, not bundle identity.

A safety counterexample or an actually executed joint M36/M38 counterexample is
a verification failure. Missing/unknown/vacuous M35/source evidence or an
unsatisfied requested proof is incomplete. A bounded cover miss is non-failing,
and unavailable advisory candidate evidence neither changes M39 eligibility nor
makes an otherwise complete joint run incomplete.

Likewise, merely detecting Clash, Verilator, Yosys, SymbiYosys, or Z3 does not
create a tool-execution record. A tool record represents an actual normalized
invocation and includes its role, exact reported version, path-free command
shape, result status and exit code, logical outputs, and proof mode/depth when
applicable. Tool output paths must resolve to files already published by the
build.

## Counterexample and source attribution

Failed M35, M36, and M38 results retain their typed counterexample metadata and
ZLang `SourceOrigin`. The evidence identity contains a SHA-256 digest of the
complete counterexample data, including the raw trace, while human/JSON report
details include concise metadata such as the property, cycle, semantic signal,
backend pair, and artifact hashes. The potentially large raw trace is not
duplicated inline in every report.

Source origins remain attribution, not semantics: they include the logical
source unit, source digest, span, and construct, survive manifest JSON
round-trips, and are excluded from deterministic evidence and build identities.
Moving an unchanged locked project therefore does not change its build identity.

## CLI publication

The intended publication forms are:

```sh
zlang design.zhl --systemverilog build/design.sv \
  --evidence-report build/evidence.json \
  --evidence-format json \
  --build-manifest build/zlang-build.json
```

`--evidence-format text|json` selects deterministic human-readable or JSON
evidence. `--evidence-report` publishes that report. `--build-manifest`
publishes the whole-build join after requested outputs have been written and
their hashes validated. The manifest is fsynced to a same-directory temporary,
renamed atomically, its physical publications are validated again, and the
parent directory is fsynced; a detected intervening output mutation removes
the manifest. A whole-build manifest requires a real backend product;
report-only or check-only commands do not fabricate a successful build.

The build identity includes locked source/dependency hashes, both canonical IR
identities, normalized implementation request/policy and optional profile,
independent backend records, actual tool executions, reports, evidence, and
stable metadata. Ordering is canonical. Host paths, timestamps, wall time, cache
hit order, Python insertion order, and other volatile runtime state are not part
of it.

## Current limitations

- Formal execution is opt-in and has distinct triggers. A non-`off` policy alone
  runs the existing selection-time M39-to-M36 route. `--verify` with policy
  `off` executes M35/source safety and covers. Bundle-only publication creates
  immutable safety/cover inputs and the base compiler plan but does not prepare
  or execute selected-candidate M36/M38. Joint `--verify` plus a non-`off`
  policy enriches the plan and may execute compatible selected-candidate Clash
  M36, direct-SV M36, and M38 routes.
- A base bundle replays only immutable M35/source safety and cover jobs. When
  selected-candidate M36/M38 routes were prepared for publication, their strict
  path-free typed inputs are stored as hash-validated companion records;
  `zlang-verify` executes those frozen routes without source or M39 reselection.
  Solver results remain external run evidence and are never embedded as trusted
  source facts.
- This orchestration does not invent new M36, M38, or M39 relations; all results
  still use their existing typed compiler-owned routes and distinct status
  vocabularies. M38 is advisory for M39 eligibility, but an actually executed
  M38 counterexample is a verification failure.
- M38's current miter validator accepts the original BackendArtifact v2
  intersection. Newer feature-bearing artifact versions may be recorded in a
  whole-build manifest, but that does not make them M38-compatible. An M38 claim
  is included only when a typed `CrossBackendResult` already exists for a
  compatible v2 artifact pair.
- A whole-build manifest does not establish protocol observational equivalence,
  hierarchical M36/M38, liveness, CDC refinement, or any new formal relation.
- Recursive register/FIFO/request-response/CSR goals require complete published
  observations. Typed hierarchy ownership identifies true root-environment
  assumptions and publishes one deduplicated feasibility cover per exact
  physical instance/assumption/domain/route set. Internal, mixed, unresolved,
  or unavailable ownership remains explicit incomplete evidence; assumptions
  are never dropped to make a harness executable.
- Tool availability without execution is not recorded as success.
- Source attribution is intentionally not sophisticated waveform
  reconstruction; counterexamples retain semantic IDs, origins, and raw trace
  metadata for downstream tools.

These boundaries preserve the existing formal-infrastructure freeze while
making build and evidence claims reproducible, attributable, and mechanically
auditable.
