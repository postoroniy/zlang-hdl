# Backends, CLI, and tooling

ZLang performs parsing, semantic analysis, canonicalization, and implementation
selection before backend emission. Backend behavior is not the source of language
semantics.

## Backend policy

Clash is the current primary/general backend and independent implementation
oracle. Direct SystemVerilog is a stable supported secondary backend for a
validated, fail-closed subset. `--experimental-systemverilog` remains a
compatibility alias for `--systemverilog`.

Every supported direct-SV example root emits deterministic RTL and passes strict
Verilator lint. Unsupported IR raises a structured backend error and does not
publish an artifact. The exact current matrix is maintained in
[Direct SystemVerilog](direct-systemverilog.md); do not infer full language
coverage from one successful design.

Both backends consume typed IR and publish BackendArtifact manifests with
semantic bindings separate from physical RTL locators. Standard buses are
ordinary `.zl` library modules, not AXI/APB-specific backend dispatch.

For one non-default physical domain, both backends consume the same typed
[clock/reset contract](physical-clock-reset-contract.md). Concise `async reset`
creates one root-owned asynchronous-assert/two-edge-synchronous-release
conditioner: direct SV emits the two `ASYNC_REG` stages, while Clash uses one
top-wrapper `resetSynchronizer`. Child components receive the conditioned reset.
BackendArtifact version 10 binds the external clock/reset ports to that exact
contract; legacy synchronous artifacts retain their prior version and text.

## Public RTL boundary

The selected ZLang top has one physical ABI in both backends. `TopPhysicalABI`
recursively exposes struct fields and tuple components as named leaves and
preserves vectors as native unpacked SystemVerilog arrays. Tuple components use
deterministic `itemN` path segments. There is no packed/leaf mode and no
compatibility switch. Scalar and protocol ownership, field paths, array shapes,
packing slices, clock/reset domains, and manifest identities all come from the
same typed projection.

Direct SystemVerilog emits a private packed `<Top>__zlang_core` only when a
boundary conversion is needed. Clash 1.11 always packs `Vec` in generated
Verilog, including its SystemVerilog mode, so `--verilog-dir` invokes Clash with
an explicit component prefix and publishes two files: public `<Top>.sv` and
packed `zlang_core_<Top>.v`. The wrapper uses typed packing metadata; it does
not parse or rename generated RTL. In both paths `v[0]` occupies the
most-significant packed region. Internal child/component ABIs are unchanged.

The versioned [whole-build manifest](whole-build-manifests.md) joins locked
sources, high-level/selected IR identities, independently planned backend
artifacts, companion files, actual tool executions, reports, and typed evidence.
It keeps physical implementation identities separate from canonical semantics.

Implementation graphs distinguish backend-independent generic structure from
a backend-realized physical plan. Their timing metadata also distinguishes a
known latency from timeless or unknown behavior; an integer zero is never used
as proof that unknown stateful behavior is same-cycle.

Named project [implementation profiles](implementation-profiles.md) normalize
external target/backend policy with legacy source exploration forms. Clash and
direct-SystemVerilog plans are reported independently, so a physical direct-SV
resource graph is never attributed to Clash.

## Core CLI

```text
zlangc SOURCE [options]
```

| Option | Action |
| --- | --- |
| `--check` | Parse and semantically validate through the demand-driven session; do not construct planning, formal, report, or backend products. |
| `--top NAME` | Select an elaboration root. |
| `--project PATH` | Select a locked `zlang.toml` project instead of parent discovery. |
| `--profile NAME` | Select one strict implementation profile from the project. |
| `-o`, `--output PATH` | Write Clash source. |
| `--verilog-dir DIR` | Run Clash and retain generated Verilog. |
| `--constraints-xdc PATH`, `--constraints-sdc PATH` | Publish one typed single-domain clock constraint beside the selected physical ABI. |
| `--verilator-lint` | Lint retained Clash Verilog; requires `--verilog-dir`. |
| `--systemverilog PATH` | Write direct SystemVerilog for the supported subset. |
| `--experimental-systemverilog PATH` | Compatibility alias. |
| `--target NAME` | Select a compiler-shipped target instance. |
| `--target-architecture NAME` | Select one source-described architecture template explicitly. |
| `--target-architecture-mode generic\|preferred\|required` | Choose generic-only, fallback, or fail-required physical selection. |
| `--target-evidence-policy estimate_only\|measured_preferred\|measured_required` | Control whether compatible measured/routed evidence is optional or required. |
| `--implementation-manifest PATH` | Write the selected-resource `BackendArtifact` manifest. |
| `--diagnostic-format text\|json` | Select compatible text or versioned structured errors. |
| `--source-map PATH` | Write a hash-bound origin sidecar for one explicit backend output. |
| `--evidence-report PATH` | Write deterministic typed semantic/timing/formal evidence. |
| `--evidence-format text\|json` | Select the evidence report representation. |
| `--build-manifest PATH` | Write the validated whole-build manifest after backend publication. |
| `--verify` | Publish a per-goal routed verification bundle and execute its executable safety and bounded-cover jobs. With a non-`off` formal policy, also execute selected-candidate M36/M38 evidence. |
| `--verification-bundle DIR` | Publish the immutable, hash-validated bundle for later replay. A base publication contains safety/cover jobs; a joint prepared plan may also include exact selected-candidate M36/M38 companions. Publication itself executes neither route. |
| `--verification-report PATH` | Write the raw safety/cover run report, or the joint compiler wrapper when candidate M36/M38 also ran. |
| `--verification-work-dir DIR` | Retain generated SBY inputs, solver logs, and traces for `--verify` outside the immutable bundle. |
| `--verification-format text\|json` | Select verification report rendering. |
| `--verify-require checked\|proven` | Require bounded safety checking or a complete proof; bounded evidence never satisfies `proven`. |
| `--implementation-policy-report PATH` | Write normalized source/profile policy and semantic regions. |
| `--backend-implementation-report PATH` | Write independent Clash/direct-SV plan statuses. |
| `--formal-harness PATH`, `--formal-sby PATH` | Generate the existing M35 checker/SymbiYosys inputs; generation alone is not proof execution. |
| `--formal-depth N` | Set bounded depth for M35/M39 and for joint candidate M36/M38 execution. |
| `--formal-policy off\|available\|required_bmc\|required_proven` | Select the frozen M39 candidate eligibility policy. |
| `--formal-max-candidates N` | Bound selection-phase M39 execution. |
| `--formal-timeout SECONDS`, `--formal-cache DIR` | Set the formal timeout and the shared namespaced prepared/M35/M36/M38/M39 cache. |
| `--formal-jobs N` | Execute independent verification-bundle safety/cover jobs and independent selected-candidate sites concurrently. Stages within one candidate site remain ordered; report order remains deterministic. |
| `--synthesis-report PATH`, `--synthesis-cache DIR` | Publish/cache optional measured Yosys candidate evidence. |
| `--synthesis-target generic-lut6` | Select the current bounded Yosys characterization target. |
| `--clash PATH` | Select the Clash executable. |
| `--verilator PATH` | Select the Verilator executable. |
| `--yosys PATH` | Select the Yosys executable. |
| `--verbose` | Print success/artifact notices on stderr. |

Clash discovery is deterministic. An explicit `--clash PATH` applies to that
command. Otherwise the compiler checks `ZLANG_CLASH`, then `clash` on `PATH`,
then searches the checkout named by `ZLANG_CLASH_ROOT`. An explicitly selected
but invalid executable is an error; the compiler does not silently choose a
different installation. Verilator, Yosys/SBY, solver, and Icarus tools are
resolved from `PATH` by the commands that require them.

Artifact/report sinks suppress implicit Clash stdout. Selection flags such as
`--top` alone do not. Diagnostics always use stderr and a nonzero exit status on
failure. See [structured diagnostics](structured-diagnostics.md) and
[generated source maps](generated-source-maps.md).
Target and M39 details are in the
[target-aware planner](high-level-target-aware-architecture-pipeline-planner.md)
and [optimization/formal guide](optimization-formal.md).

`--verilog-dir` always publishes the selected ZLang top as a SystemVerilog
public-boundary wrapper. Struct members are individual named ports and vectors
are native unpacked arrays; a private `zlang_core_<Top>` component retains the
packed `Vec` ABI generated by Clash 1.11. The wrapper is derived from
`TopPhysicalABI` and Clash's explicit component-prefix option, never by parsing
or renaming generated RTL. BackendArtifact locators name the public wrapper
ports. Its artifact hash continues to identify the generated Clash source;
the whole-build manifest independently hashes both final wrapper and core RTL
files. This separation avoids treating a final-RTL wrapper as a ROM companion
or changing semantic/backend identity.

All explicit output files and compiler-owned output/cache directories must be
pairwise disjoint and outside every resolved source, project, lock, dependency,
and stdlib input. Generated RTL and companion publication also refuses symlinked
or non-regular destinations. See the complete
[whole-build publication contract](whole-build-manifests.md#published-files-and-companions).

## Generated artifacts and reports

| Option | Artifact |
| --- | --- |
| `--csr-markdown`, `--csr-json` | CSR software views from typed CSR IR. |
| `--contracts-sva` | Bindable contract checker. |
| `--high-level-ir` | Canonical high-level IR. |
| `--optimization-ir` | Selected architecture IR. |
| `--saturation-report` | Frozen pure-expression rewrite report. |
| `--implementation-report` | Implementation-choice explanation. |
| `--cost-report` | Candidate costs and deterministic selection. |
| `--pipeline-report` | Automatic pipeline candidates/result. |
| `--architecture-report` | Reduction architecture candidates/result. |
| `--exploration-report` | Unified exploration staging/rejection/result. |
| `--synthesis-report`, `--synthesis-cache` | Optional Yosys evidence and cache. |
| `--implementation-manifest` | Direct-SV implementation/resource artifact metadata. |
| `--source-map` | Generated-line to typed ZLang-origin sidecar. |
| `--evidence-report` | Typed evidence with exact `typed_legal`, timing, bounded, proof, failure, skip, and not-run status. |
| `--build-manifest` | Deterministic build join over sources, canonical identities, products, tools, reports, and evidence. |
| `--verification-bundle` | Immutable manifest, verification IR, implementation/source map, and executable or explicitly skipped per-safety/per-cover jobs. |
| `--verification-report` | Raw v7 safety/cover results, or the joint v1 compiler wrapper with separately typed candidate M36/M38 evidence. |

Formal execution follows an exact trigger matrix:

- with none of `--verify`, `--verification-bundle`, or a non-`off` formal
  policy, compiler orchestration schedules no formal work;
- a non-`off` policy alone runs only the existing selection-time M39-to-M36
  route; it does not prepare direct-SV M36 or M38 evidence;
- `--verify` with policy `off` runs only executable M35/source safety and cover
  jobs;
- `--verification-bundle` publishes immutable safety/cover inputs and the base
  compiler linking plan, but does not execute or prepare selected-candidate
  M36/M38, even if a non-`off` policy is also supplied. Publication may still
  invoke Clash to prepare a lazy M35 fallback route, but it does not run a
  solver;
- `--verify` with a non-`off` policy enriches that plan and executes M35/source
  jobs plus the compatible selected-candidate Clash M36, direct-SV M36, and M38
  routes.

The linking plan references exact M35/source jobs and M39 candidate records by
semantic site and rank while retaining their separate result vocabularies.
Selected-candidate plans are added only by the joint `--verify` trigger, and
their execution results live in the external compiler verification report and
evidence report, not in the immutable bundle.
Legacy `--formal-harness`/`--formal-sby` remain available only for a complete
single-route safety view; mixed, cover, recursive, or incomplete views fail with
guidance to publish a verification bundle.

Feature-specific options reject sources that do not contain the corresponding
construct. Saturation requires a selected output; synthesis feedback requires
both report and cache paths. Estimates and measured results are never relabeled.
Generating a harness or SBY file never counts as proof execution; see
[whole-build manifests and evidence](whole-build-manifests.md#harness-generation-is-not-proof-execution).

Replay a published bundle without recompiling source:

```sh
zlang-verify build/verify --mode bmc --depth 20 \
  --work-dir build/verify-work \
  --format json --report build/verification-report.json
```

Replay validates every content hash before execution. It executes the bundled
M35/source safety and cover jobs only; it does not execute or reconstruct the
selected-candidate M36/M38 part of a joint compiler run. Engine, solver, depth,
timeout, tool versions, logs, and results belong to execution and do not mutate
the bundle. `zlang-verify` defaults its work directory to the sibling
`build/verify.work`; `zlangc --verify` uses `--verification-work-dir` when
provided. A work directory inside the bundle is rejected. A safety
counterexample, or any actually executed joint M36/M38 counterexample, exits
`1`. Unavailable, unknown, vacuous, or insufficient M35/source proof evidence
exits `2`; unavailable advisory candidate evidence does not by itself make a
joint report incomplete. A normal `bounded_unreached` cover result does not fail
the command. See [Optimization and formal verification](optimization-formal.md)
for the current verification contract and result meanings.

The immutable bundle contains configuration-neutral implementation and checker
sources, not a depth- or solver-specific `.sby` file. `zlang-verify` constructs
that execution configuration from its command-line options, so replaying at a
different depth does not change the bundle identity.

Bundle schema v4 and verification-IR snapshot v3 remain immutable inputs. The
current raw `zlang-verification-run-report-v7` adds a deterministic run
identity, strict plan/route provenance, retained bounded-stage evidence,
per-job tool/version and work-directory attribution, and semantic
witness/counterexample values reconstructed from retained SBY VCDs through the
bundle binding table. It also retains each exact typed clock/reset contract and
its validated BackendArtifact physical-domain identity. Generated
configurations, stdout/stderr, timeout logs,
and VCDs live below configuration- and job-specific work directories; paths and
raw log text do not participate in the run identity.

When joint selected-candidate evidence executes, the public JSON result is
`zlang-compiler-verification-report-v1`. It wraps the raw v7 safety/cover report,
the exact compiler linking plan, and separately typed M36/M38 candidate reports.
Candidate reports retain deterministic per-route M36/M38 work directories and
the discovered tool snapshot when those routes execute. Exact in-session reuse
of an M39 result also retains its recorded work root. Persistent M39 cache
payloads deliberately exclude physical paths, so a cache hit does not claim
that a stale workspace still exists. Paths remain operational metadata outside
semantic, evidence, run, and proof-cache identities.

The bundle's typed `FormalExecutionPlan` schema 2 routes each goal independently.
It records the complete assumption set, exact `ClockDomain`, observations,
comparison window, selected/artifact identities, the physical-domain identity
when a compatible BackendArtifact exists, and a complete direct-SV or lazy
Clash route. Bundle v4 jobs and run-report v7 results repeat that identity;
strict restoration rejects a corrupt digest or plan/job/result disagreement. A
harness never combines backend signal sets. Prepared routes,
M35 jobs, M36/M38 products, and M39 records use separate content-addressed
cache namespaces; only decisive results are reusable. The current orchestration
and caching rules are documented in
[Optimization and formal verification](optimization-formal.md).

Routing is also per clock/reset pair. Goals in multiple supported synchronous
domains may execute as independent jobs. For a single physical domain, existing
formal routes support rising/falling edges, synchronous or raw asynchronous
assertion, active-high/active-low polarity, and the concise two-edge
synchronized release, provided `power_up` is `unspecified`. Multi-domain
asynchronous reset and incompatible domain manifests skip only the goals that
name them. This does not introduce a cross-domain property relation or broaden
general backend domain support.

Trace publication keeps `physical_reset`, the polarity-preserving external pin,
separate from `trace:reset`, the normalized active-high effective reset used by
guards, history/fill masks, and fixed-latency comparison windows. Result
`reset_state` reports the effective view; for synchronized release it remains
asserted for the two active release edges after the physical pin deasserts.

A prepared-route cache hit avoids rebuilding that backend route; a decisive
result-cache hit avoids rerunning its solver. Avoiding both operations therefore
requires both exact recipe hits. One compilation session shares a lazy tool
resolver across bundle and candidate execution. It probes only requested
engine/solver and Clash contexts, and an all-skipped bundle run performs no
formal-tool discovery.

Recursive register/FIFO/request-response/CSR goals execute only when every
published observation and assumption can be connected. Typed hierarchy
ownership traces each requirement to the root ABI. A true external assumption
set receives one deduplicated feasibility cover for its exact physical
instance/domain/backend route; internal, mixed, unresolved, or unavailable
ownership remains explicit incomplete evidence. No assumption is silently
discarded.

`--mode prove` and `--verify-require proven` always run BMC first. The prove
attempt starts only when every safety job is `bounded_pass`. Covers run once in
the BMC stage and are not rerun; an unrelated missed or unavailable cover does
not block a clean safety proof, while an unwitnessed or unavailable feasibility
cover makes its dependent safety result vacuous/unknown and therefore blocks
proof. The final merged report retains the cover result, so it can still be
incomplete. BMC
success remains `bounded_pass` and never becomes `proven` merely because a later
stage was requested.

ROM-backed direct-SV formal artifacts publish exact companion images under
`implementation/companions/`, and each executable job lists those companions
as hash-validated inputs. If direct-SV formal emission is unavailable, the
bounded structured Clash route may finalize real generated Verilog, validate
public and recursive observation ports, and republish one deterministic
immutable artifact/hash for scalar/public and register-observation cases.
Unsupported aggregate/protocol/hidden shapes remain explicitly non-executable;
the fallback never reconstructs bindings from generated names.

## Standard library

`import std.bus.reg` maps the stable logical module name to the compiler-shipped
physical source `stdlib/bus/reg.zl`. Imports are transitive, dependency-ordered,
and content-hashed. This is not an arbitrary filesystem or user-package import
mechanism.

Current shipped areas include fixed-point helpers, generic complex arithmetic,
RegBus, AXI4-Lite, APB, AXI-Stream, Wishbone, target/resource descriptions, and
validated bridge/CSR components. Inspect the shipped [`stdlib/`](../stdlib/)
sources and the [hierarchy/protocol guide](hierarchy-protocols.md).

## Locked projects

External logical imports are available through versioned `zlang.toml` and
`zlang.lock`. `zlang-lock update --project PATH` is the only operation that may
fetch/populate dependencies. Ordinary `zlangc` project compilation is offline,
read-only, and rejects dirty or unavailable lock content. See
[projects and dependencies](projects-dependencies.md) for the exact path/Git,
identity, cache, and current deferral rules.

## Editor support

The repository-owned VS Code extension is
[`editors/vscode/zlang-vscode`](../editors/vscode/zlang-vscode). It provides
lexical highlighting for implemented syntax classes. Highlighting cannot prove
that a name, type, width, domain, protocol connection, or backend feature is
valid; `zlangc --check` is the semantic validator.

## Testing and external tools

Run focused tests serially while debugging and the complete suite in parallel:

```sh
.venv/bin/python -m pytest -q tests/semantic/test_file.py
.venv/bin/python -m pytest -q -m conformance
.venv/bin/python -m pytest -n 2 --dist=loadscope -q \
  -m 'conformance or toolchain_smoke'
.venv/bin/python -m pytest -n 8 --dist=loadscope -q
```

The broad gates reuse one authoritative language-tour catalog and compile each
example source/top once per test-module run. They supplement rather than replace
the unique negative, behavioral, mutation, and formal assertions in the full
suite. See [the test strategy](testing.md).

The real integration suites discover Clash, Verilator, Yosys/SymbiYosys, and Z3.
Genuine tool absence is an explicit skip, never a pass. Generated RTL, formal
workspaces, and tool outputs use isolated temporary directories in parallel runs.

`zlang-compare-backends` runs the repository's bounded Clash/direct-SV evidence
suite. QoR claims require the same target, constraints, and numerical semantics;
estimated cost is not physical evidence.

### Emitter architecture boundary

Both renderers consume the same typed hierarchy, endpoint/connection,
`TopPhysicalABI`, and physical-type facts, but their component ABIs deliberately
remain independent. Clash closed-component specialization and bundled
application/projection live in `zlang/backend/clash/hierarchy.py`; direct-SV composed
recursion, named-port routing, request/response ledgers, and FIFO helpers live in
`zlang/backend/systemverilog/composed.py`. Neither backend-local module imports the
other renderer or its parent emitter.

Before either renderer may publish a `BackendArtifact`, the shared
`ModuleFeatureInventory` enumerates every concrete assignment, local, state and
storage entity, CSR block, protocol/aggregate endpoint and member, connection,
request/response ledger, and child instance. Backend contributors must claim
each entity exactly once. A missing, duplicate, or unknown claim aborts emission
instead of publishing partial RTL. This is exact coverage accounting for the
selected module, not a claim that arbitrary future combinations compose.

This is intentional rather than missing deduplication. Haskell `Signal`
application/record projection and SystemVerilog module/named-port wiring have
different ordering, reset, and normalization constraints. Shared facts belong
in backend-independent IR; generated-language spelling and component policy do
not.

## Current validation snapshot

The exhaustive direct-SV corpus currently discovers **80 `.zl` files and 165
module roots**: 150 standalone roots emit artifacts and pass strict Verilator
lint, while 15 generic/hierarchical children are exercised through concrete
parents. No discovered root is on an unsupported allow-list. This count is an
acceptance snapshot, not a promise that an arbitrary future IR shape is covered;
the emitter remains fail-closed. See the
[root-by-root contract](direct-systemverilog.md#exhaustive-example-matrix).

The machine-readable release minimum and zero-skip policy live in
[`release/status.json`](../release/status.json) and are checked against two
complete CI JUnit reports. The
FFT512 persistent-hierarchy replay is a routine default-suite test: its
1,033-cycle scenario completes in about 11 seconds with roughly 84 MiB RSS and
matches the same frozen oracle as direct-SV and real Clash RTL. The ordinary
real Clash, direct-SV, Verilator, Yosys/SBY, and Z3 integration paths also run
in the default suite. Exact numerical and architecture evidence is
recorded in the [FFT guide](../examples/fft/README.md) and
[802.11a report](80211a-transmitter-validation.md).
The repository-wide dated summary is maintained in
[current-language-status.md](current-language-status.md); older per-slice
counts in design records remain historical evidence.

## Current boundaries

The project layer deliberately has no registry/semver solver, mutable revisions,
or implicit compile-time fetching. The compiler also has no arbitrary temporal
DSL, full AXI4, or automatic backend-by-IR selection. Nominal enums and immutable
initialized ROM are supported within their documented boundaries. Backend-
specific limitations are documented explicitly and remain fail-closed.
