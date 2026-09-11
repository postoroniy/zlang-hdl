# Optimization and formal verification

ZLang keeps value equivalence, timed equivalence, architecture alternatives,
protocol observational equivalence, backend equivalence, cost evidence, and
formal eligibility separate. Optimization never changes source semantics merely
because a backend happens to synthesize two expressions similarly.

## Canonical and selected IR

Compilation retains a high-level typed representation and a selected
architecture representation. Metadata includes canonical type, width,
signedness, latency, initiation interval, domain, purity/effects, source origin,
and separate estimated/measured cost evidence.

The pure e-graph layer uses the pinned `egglog==13.2.0` engine for a bounded,
type-safe scalar rewrite subset. It covers exact integer/fixed arithmetic,
bitwise/shift, compare/mux, resize and wiring nodes. Fixed conversion is an
opaque quantization boundary. Reassociation of ordinary carry-growing adds,
movement across rounding/rescale, state, timing, protocols, storage, rules, and
CDC are excluded. Source `equiv` declarations register only accepted exact
same-cycle value rules; they are not assertions or temporal equivalence.

## One implementation-policy path

The staged flow is explicit; egglog is a typed value-alternative producer, not
the pipeline or architecture search engine:

```text
typed value IR
    -> optional egglog pure-value alternatives
    -> typed computation DAG
    -> bounded generic/resource covering
    -> target-aware exact-N fixed-latency scheduling
    -> M28 deterministic cost extraction
    -> optional M39 authoritative M36 semantic-reference proof gate
```

> **Current backend policy (2026-09):** Direct SystemVerilog is the only
> production RTL backend. Clash/M38 material below is historical compatibility
> evidence and is not executed by the current release path.

M30 supplies validated latency/II relations for eligible candidates. That
metadata validation is not, by itself, a formal proof.

The [arithmetic exploration tutorial](../examples/verification/math-exploration.md)
demonstrates an exact eight-product expression, topology-only selection versus
an internally registered pipeline, real latency-aware equivalence/mutations,
and independent FPGA timing measurement. Solver success and estimated frequency
must not be reported as routed 100 MHz timing closure.

The canonical source form is `implement`; all compiler-selected forms normalize
through the same typed implementation-policy/extraction infrastructure:

```zlang
y = implement {
    dot(a, b)
    intent {
        latency <= 4
        ii == 1
        dsp <= 8
        minimize lut
    }
}
```

`implement` selects applicable exact value, reduction/DSP, and (when a legal
clock/reset context exists) fixed-latency pipeline candidates. It does not
enable unsafe reassociation, CDC, protocol adaptation, variable-II sharing, or
general retiming implicitly. Those remain compatibility/profile-controlled
features with their existing legality checks.

The remaining source forms are:

- `choice(...)` for explicit equivalent arms and bounded cost selection;
- protocol `transform pipeline(auto, ...)` for the existing globally-stalled
  ready/valid transform.

The scalar spellings `architecture(auto)`, `pipeline(auto)`, and `explore` are
retired and fail with migration diagnostics. Use:

```zlang
y = implement {
    dot(a, b)
    intent {
        latency <= 4
        ii == 1
        dsp <= 8
        minimize lut
    }
}
```

Hard constraints are never silently relaxed. Estimated cost and measured
synthesis evidence stay distinct. Automatic protocol adaptation, CDC insertion,
general retiming, variable latency, and II-changing sharing are not inferred.

### Explicit choices

```zlang
y = choice(auto, minimize=lut, dsp<=1, latency<=1, ii<=1) {
    mul_add => pipeline(1) { a * b + c }
    dsp_mac => pipeline(1) { a * b + c }
}
```

`dsp_mac` is mapping intent, not evidence that a physical DSP was used.

### Exact and selected timing

```zlang
y = implement {
    a * b + c * d + e * f + g * h
    intent { latency <= 3 ii == 1 dsp <= 4 fmax >= 100 }
}
```

`implement` considers pipeline candidates only in a legal clock/reset region
when the intent explicitly permits positive latency. `pipeline(3) { expr }`
remains exact three-cycle hardware semantics and is never lowered to an
implementation preference.

For supported pure scalar DAGs the physical record is one
`ScheduledValueGraph`: operations, exact stage assignment, generic or resource
bindings, resource-local/fabric cuts, balancing delays, latency, II and cost
provenance. Egglog never places these cuts. On Xilinx 7-Series the target
planner can cover standalone multiply, MAC/add-sub, preadd-multiply and ordered
signed-product cascades with DSP48E1; uncovered operations remain fabric.
Unknown target timing cannot satisfy an Fmax constraint, and structural cost is
never reported as a measurement.

Only frozen expression shapes are accepted. Candidate timing is checked and
the result is a concrete fixed-latency expression. Target-aware fixed-FIR
planning is similarly bounded and keeps final fixed-point quantization outside
partial products/reduction nodes.

The first protocol-aware form is an explicit ready/valid transform:

```zlang
input -> output {
    transform pipeline(auto, latency<=3, ii==1, fmax>=100) {
        input.payload.a * input.payload.b
          + input.payload.c * input.payload.d
          + input.payload.e * input.payload.f
          + input.payload.g * input.payload.h
    }
}
```

This bounded form accepts one ready/valid input and output in one synchronous
domain and a pure existing M31 product-reduction kernel. The selected plan owns
one global-clock-enable stall policy: all data registers and its valid chain advance
together only when the output is empty or ready. Its contract is therefore
`minimum_unstalled_latency=L`, `ii_no_stall=1`, capacity `L`, and explicitly
variable wall-clock latency under backpressure. It is not an M30 fixed-latency
relation. User registers, rules, storage, protocol-control captures, adapters,
crossings, and independently elastic stages are rejected in this first slice.

Direct SystemVerilog lowering is the production route. A preferred physical-
resource request may report a generic fallback; a required physical route fails
until every selected resource site explicitly advertises a compatible common
clock-enable/stall input. Existing M35 ready/valid stability remains
applicable. M36 is supported where direct-SV bindings exist; M38 is retired.
M39 `available` records an explicit skipped route and required proof policies
fail closed.

Target and resource descriptions under `std.target.*` and `std.arch.*` are
compiler-shipped source. Functional modules do not name vendor registers or
primitives. Manual `required`, `preferred`, and `generic` architecture modes
determine failure/fallback behavior; selecting a target alone does not start
automatic search.

## First-class verification goals and contracts

Verification declarations are a verification-only overlay over the typed
module. Standalone safety and bounded-reachability goals are named:

```zlang
assert count_within @ clk {
    count <= DEPTH
}

cover reaches_done @ clk {
    state == State.Done
}
```

Related requirements and goals can share a contract scope:

```zlang
contract fifo_behavior @ clk {
    require legal_input {
        input_length <= 1500
    }

    assert count_within {
        count <= DEPTH
    }

    ensure output_shape {
        !output.valid | output.payload.ok
    }

    cover full {
        queue.full
    }
}
```

`assert` and `ensure` are same-cycle safety obligations. `cover` is bounded
reachability and never constrains the design. `require` is an environment-owned
precondition local to its contract; all requirements in one scope are conjoined
and gate that scope's goals. An `ensure` must observe at least one
implementation-owned public output and cannot depend on hidden state.
Goal/clause names are mandatory and unique. `@ clk` may be omitted only when
exactly one clock/reset domain exists; reset is inferred from that domain and
suppresses sampling.

The existing forms remain source-compatible:

```zlang
assume bounded @clk disable iff rst {
    (a < 8) & (b < 8)
}

guarantee sum_matches @clk disable iff rst {
    y == a + b
}
```

Legacy `assume` normalizes into a module-global requirement and `guarantee` into
a module-global assertion. Every body has ordinary ZLang type `bit`.
Cross-domain references and unsupported temporal forms are rejected. This
slice does not add arbitrary SVA/SMT, liveness, eventual delivery, fairness, or
temporal implication syntax.

An `assume` may reference environment-owned leaves only. In particular, a
ready/valid or request/response `.transfer` combines an environment input with
an implementation output and is therefore not a legal assumption predicate.
`guarantee` remains free to observe both sides. Executable declarations lower
to typed structured predicates; report strings and RTL names are never parsed
to recover their meaning.

Verification declarations have a separate identity and do not change
production RTL text/hashes, high-level or selected hardware identities,
optimization, M36/M38 relations, or M39 cache semantics.

The simulation `VerificationMonitor` samples after combinational settle and
before edge commit. Failed active `assert`/`ensure` goals are source-attributed
DUT failures. Violated `require`/`assume` clauses are recorded as environment
violations and gate their dependent goals. Cover records only its first witness
cycle; a missing simulation witness is not a failure.

`--contracts-sva` emits supported bindable safety goals through the same
structured predicate meaning as M35. Missing observations remain explicitly
non-executable rather than falling back to a second expression walker.

Each non-empty assumption set has a feasibility cover. A compile-time false
requirement is rejected; a dynamically unwitnessed requirement makes dependent
safety success `unknown` with a vacuity diagnostic rather than a pass.

Executable formal routes consume the exact typed physical-domain contract.
With `power_up unspecified`, M35/source safety and cover, existing bindable
recursive M35 observations, fixed-latency II=1 M36, compatible M38, and the
corresponding M39 policies support rising/falling edges, synchronous or raw
asynchronous assertion, both polarities, and the existing two-active-edge
synchronized release. Asynchronous formal execution remains single-domain;
multiple synchronous domains may still produce independent goal-local jobs.
Same-cycle pure candidate equivalence remains reset-independent.

The checker normalizes external polarity to one active-high effective reset.
For synchronized release, checker history, property guards, feasibility
covers, fill masks, and comparison windows stay reset-masked for both release
edges after the raw pin deasserts. Formal traces publish both `physical_reset`
(the raw external pin) and `trace:reset` (the effective reset); result
`reset_state` is the effective view. Unsupported contracts are never coerced
to the legacy reset model.

### Execution and immutable bundles

Start with the [runnable verification tutorial](../examples/verification/README.md)
for a proved state invariant, an intentionally rare overflow counterexample,
scoped assumptions and RV stall checking. It includes the important case where
shallow BMC passes a broken design and deeper replay finds the defect.

```sh
zlang design.zhl --verify
zlang design.zhl --verification-bundle build/verify
zlang-verify build/verify
```

`--verification-report PATH` plus `--verification-format text|json` publishes a
structured result. `--verify-require checked|proven` selects the requested
safety level. `--formal-jobs N` controls independent safety/cover bundle jobs
and independent selected-candidate sites. Dependencies inside one candidate
site remain ordered and report ordering remains deterministic.
`--verification-work-dir DIR` and replay's `--work-dir DIR`
retain generated SBY inputs, logs, and VCDs outside the immutable bundle. A
bundle contains a hash-validated manifest, structured verification IR,
implementation/formal artifact, source map, separate safety/cover harnesses,
and exact ROM companions when needed. Engine, solver, depth, timeout, tool
versions, logs, and results belong to replay execution rather than the
immutable source bundle identity; the replay tool regenerates
execution-specific SBY configuration.

The immutable inputs currently use bundle schema v4 and verification-IR
snapshot v3. M35/source execution uses run-report schema v7, which adds a deterministic
run identity, strict route provenance, staged BMC/prove evidence, and per-job
work/tool attribution. Joint compiler execution wraps that raw report and
candidate M36/M38 evidence in `zlang-compiler-verification-report-v1`. Retained VCD frames are mapped
through the bundle bindings to semantic signal IDs for source-facing witness
and counterexample values. Work paths and raw logs remain reproducibility data,
not semantic or run identity.

A `proven` request is never a direct shortcut to prove mode. BMC executes first
at the requested depth; every safety job must be `bounded_pass` before those
safety jobs enter prove mode. Covers execute once and are not rerun. An
unrelated cover miss/skip does not block proof, while a feasibility-cover miss
first makes its dependent safety job `unknown` and therefore blocks proof.

Safety statuses remain `failed`, `bounded_pass depth=N`, `proven`, `unknown`,
and `skipped`. `bounded_pass` is never proof. Cover has a distinct vocabulary:
`witnessed cycle=N`, `bounded_unreached depth=N`, `unknown`, and `skipped`.
`bounded_unreached` is neither `proven` nor `unreachable`, and an ordinary cover
miss does not make the command fail.

Exit status `0` means the requested safety level was satisfied. A M35/source
counterexample or any actually executed M36/M38 counterexample returns `1`.
M35 unknown/skipped/vacuous execution, tool/configuration failure, or a
requested proof with only bounded evidence returns `2`. A cover miss alone is
not a failure, and unavailable advisory candidate evidence does not alter M39
eligibility.

Verification-bundle construction uses a typed compiler-owned execution plan.
Current production execution is direct-SystemVerilog plus the independent
semantic reference. Clash/M38 routes described in older records are retained
only as historical compatibility evidence.
Every goal records its exact `ClockDomain`, BackendArtifact physical-domain
identity when an artifact exists, complete scoped assumptions, required
semantic observations, comparison window, and either one complete route or one
structured skip reason. Formal plan schema 2 includes both domain fields in
`plan_identity`; bundle v4 jobs and run-report v7 results repeat them and reject
contract/manifest disagreement. Direct-SV is the only production route per
goal; an unavailable route is an explicit skip. Signals from different
backends are never mixed in one harness.
Goals in distinct supported synchronous domains become distinct jobs with
domain-local bindings. A supported asynchronous contract executes only in a
single-domain module. An unsupported domain skips only goals that name it; no
cross-domain temporal relationship is inferred and no general multi-domain
backend claim follows from this routing.
ROM `$readmemb` inputs are immutable companions. Existing recursively published
register/FIFO/request-response/CSR observations participate only when their
complete binding set exists. Typed hierarchy ownership traces recursive
requirements to the root ABI. True external assumption sets receive one
deduplicated feasibility cover for the exact physical instance/domain/route;
internal, mixed, unresolved, and unsupported shapes remain explicit incomplete
routes.

The exact source, IR, binding, simulation, vacuity, bundle, and result contract,
including the shared planning, routing, caching, comparison-window, and
triangular-evidence rules, is documented in this guide.

## Formal layers

- **M35 safety properties** originate from backend-independent semantic/selected
  IR and execute only when required observations have explicit backend bindings.
- **M36** compares supported same-cycle or fixed-latency II=1 selected
  implementations against an independent semantic reference.
- **M38** cross-backend comparison is retired with Clash. Historical reports are
  preserved for audit but are not current evidence.
- **M39** can gate deterministic candidate selection with policy `off`,
  `available`, `required_bmc`, or `required_proven`. The compiler-owned route
  materializes the frozen M36 canonical reference, compiles the selected typed
  candidate through direct SystemVerilog, validates explicit bindings, emits the latency-aware
  miter, and executes SBY/yosys-smtbmc. This applies to canonical `implement`
  regions in the existing M36 subset. Each region publishes the same structured
  M39 records into CLI evidence and whole-build manifests.

Variable-latency elastic ready/valid transforms are deliberately outside the
M36/M38 fixed-latency relation. They never enter that route by treating their
minimum unstalled latency as wall-clock latency.

`available` executes only the candidate selected by unchanged one-based M28
rank and records its result without changing static eligibility, including when
the advisory run finds a counterexample. Required policies walk the same exact
rank: a failed candidate is excluded and the next is tried; unknown/timeout
stops selection with attempted-record diagnostics. `required_proven` retains a
separate BMC stage before its prove attempt. M38/direct-SV evidence remains
optional and never participates in M39 eligibility.

Decisive M39 results are route-bound data, not trusted callback booleans. The
property, harness, assumptions, backend route, semantic-reference artifact,
implementation artifact, engine, mode, and depth must all match the
cache identity before a candidate can become eligible. Exact reuse additionally
matches stage policy, timeout, tool snapshot, dependencies, and compiler schema.
Failed results require typed counterexample metadata; non-failed results reject
it. Decisive M36/M38 evidence requires a positive depth. Missing
`yosys-smtbmc` is reported like any
other genuinely unavailable formal tool and never becomes false success.
An `unknown` result or timeout in a required mode terminates the selection: the
compiler never lets solver runtime become an implicit architecture objective.

`bounded_pass` means no counterexample was found through the stated BMC depth;
it is not an unbounded proof. Only `proven` satisfies `required_proven`.
`failed`, `unknown`, and `skipped` do not satisfy required policies.

Semantic analysis creates candidate spaces but runs no backend or solver. A
compiler-owned candidate-site ledger carries stable site identity and exact
rank into the selection phase, where `--formal-policy` executes the M39 route.
Bundle-only publication records one strict compiler-owned linking plan over the
exact verification goal plan, candidate-site ledger, and retained M39 records.
Joint `--verify` with a non-off policy enriches that immutable plan with the
selected-candidate M36/M38 plan before execution; candidate results are emitted
afterward in the combined report/evidence. Bundle publication may additionally
freeze the exact selected-candidate M36/M38 inputs as hash-validated, path-free
companions; `zlang-verify` then executes those inputs without source compilation
or M39 reselection. Base bundles contain no candidate replay inputs.
The common evidence report validates those links without merging result types.
Repeated candidate implementations are associated by semantic site and rank,
not by candidate identity alone. With policy `off`, the ledger remains visible
but there are no M39 attempt/evidence records.
Candidate execution uses deterministic recipe-addressed work roots below the
external verification work directory. An exact in-session M39 reuse retains its
recorded work root; persistent M39 cache data deliberately excludes physical
paths, so a cache hit never claims that an old workspace still exists. These
paths and the discovered candidate tool snapshot are operational report data,
not proof, cache, or compiler-verification-report identity. Parallel sites with
an identical proof recipe share the same provider-owned workspace and retain
the same complete attribution.
`--formal-harness` and `--formal-sby` remain separate M35 artifact-generation
options; writing either file alone is not proof execution. An incomplete or
mixed-route compatibility view is rejected with guidance to use a verification
bundle rather than silently choosing one backend.

The current product-validation phase freezes new recursive observation families,
hierarchical M36/M38, further rule-fire observation families, and compositional
proof machinery unless a real design demonstrates a concrete correctness
requirement. Existing rule exclusivity/priority properties now consume formal-
only accepted-fire observations derived from the typed resolved schedule; the
production RTL ABI and hash remain unchanged.

The later bounded applicability closure also transports observations already
required by existing properties: parent request/response outstanding and
directional-buffer counts, receiver-credit adapter occupancy, and direct
same-domain internal ready/valid guarantees. These are formal-only typed ABI
ports or root-monolithic assertions, never inferred RTL names or substituted
environment assumptions. Automatic root assumptions receive a separate
feasibility cover, so dependent safety evidence is vacuity-checked.

Range-proven runtime vector selection is available to the existing same-cycle
predicate IR. One separate compiler-owned helper can derive a whole-root
same-cycle M36/M38 triangle for exactly one pure scalar child by following
typed hierarchy bindings and producing formal-only Yosys namespaces. It does
not flatten production RTL and does not authorize state, protocols, storage,
arrays, nesting, or aggregate boundaries. Protocol-valued and aggregate-
protocol top boundaries fail closed in the scalar M36/M38 entry points.

See [Current language status](current-language-status.md) and
[Known limitations](known-limitations.md) for the accepted matrix and remaining
semantic boundaries.

## Reports

Useful current outputs include high-level and selected optimization IR,
saturation, implementation, cost, pipeline, architecture, exploration,
synthesis, artifact manifests, and structured formal results. See
[Backends, CLI, and tooling](backends-tooling.md) for CLI paths and
feature-specific design records for the exact schema.
