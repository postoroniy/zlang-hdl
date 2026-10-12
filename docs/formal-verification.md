# Formal verification in ZLang HDL

ZLang formal verification checks compiler-owned typed properties against a
compiler-generated Direct-SystemVerilog model. It is not a separate HDL
frontend and it does not infer requirements that were never stated.

This guide covers the current user workflow, the meaning of results, and how a
source property is transferred into the generated formal design. Runnable
examples live in [`examples/verification`](../examples/verification/README.md).

## Prerequisites

Use the checkout-local environment and put the supported external tools on
`PATH`:

```sh
make venv
source .venv/bin/activate
source /absolute/path/to/oss-cad-suite/environment
make env-check
```

Formal execution normally uses SymbiYosys, Yosys, `yosys-smtbmc`, and an SMT
solver. Z3 is the default. The exact release-tested versions are recorded in
`release/status.json`; they are evidence identities, not broad compatibility
claims.

`zlang --check` performs syntax and semantic validation only. It does not run a
solver.

## Three distinct formal surfaces

ZLang deliberately keeps three questions separate.

### Source safety and cover goals

Named declarations ask questions about one typed module:

```zlang
assert count_within @ clk {
    count <= DEPTH
}

cover reaches_full @ clk {
    count == DEPTH
}

contract bounded_request @ clk {
    require legal_input { input_length <= 1500 }
    ensure bounded_output { output_length <= 1500 }
}
```

- `assert` and `ensure` are same-cycle safety obligations.
- `cover` asks for a bounded reachability witness; it is not an eventuality
  guarantee.
- `require` constrains only its contract environment and gets a feasibility
  check so an impossible assumption cannot silently validate a guarantee.
- Legacy `assume` and `guarantee` remain supported as module-global requirement
  and assertion forms.

Every predicate is type-checked as ordinary ZLang `bit` logic. Clock/reset
ownership and cross-domain legality are resolved before formal lowering.

### Implementation-selection equivalence

`--formal-policy` controls whether an implementation candidate may be selected:

| Policy | Selection behavior |
| --- | --- |
| `off` | Do not execute candidate equivalence. |
| `available` | Execute an applicable route opportunistically and retain its honest result. |
| `required_bmc` | Select only after `bounded_pass` or stronger applicable evidence. |
| `required_proven` | Select only after unbounded `proven` evidence. |

Fixed-latency II=1 candidates use the existing semantic-reference equivalence
route where applicable. The narrow ready/valid shared `a*b+c*d` candidate uses
a different capacity-one transaction-stream BMC relation: latency 4, II 4,
capacity 1, one shared multiplier, and same-edge `RETIRE_AND_RELOAD` admission.
It may satisfy `required_bmc`; it explicitly cannot satisfy
`required_proven` because no unbounded proof is claimed.

The value e-graph supplies exact pure-value alternatives only. It contains no
ready/valid signals, FSM state, schedule, resource binding, or proof result.

### Immutable bundle replay

`--verification-bundle` publishes hash-validated formal inputs without making
the bundle identity depend on a later solver run. `zlang verify` can replay
those inputs at another depth or with another supported solver without
recompiling the ZLang source.

## From ZLang to formal Verilog

The production and verification routes share typed hardware meaning but not a
second source-language implementation:

```text
ZLang source
  -> parser and typed semantic IR
  -> typed verification predicates / semantic-reference relation
  -> selected implementation and formal planning
  -> Direct-SV BackendArtifact
       RTL text
       exact semantic-to-physical bindings
       source map and artifact identity
  -> formal-only harness or transaction miter
  -> immutable verification bundle
  -> SymbiYosys / Yosys / yosys-smtbmc / solver
  -> typed result, trace, and source-facing report
```

The formal binder consumes `BackendArtifact` bindings. It does not guess or
parse mangled RTL names. This matters for specialization, hierarchy, aggregate
ports, private helper names, and backend naming-schema changes.

For compiler work, ownership is intentionally split rather than concentrated
in one formal manager:

- `zlang/semantic/verification.py` checks source declarations;
- `zlang/ir/formal*.py` owns typed properties, observations, plans, and
  harness models;
- `zlang/formal_orchestration.py` and `zlang/formal_exploration.py` own
  execution planning, policy, evidence, and candidate gates;
- `zlang/backend/systemverilog/formal.py` renders formal-only Direct-SV;
- `zlang/formal_temporal_stream.py` owns only the narrow capacity-one
  transaction relation;
- `zlang/verification_bundle*.py` owns immutable publication, replay, and
  reports.

New routes should extend the narrow owner that matches their evidence model;
they must not recreate semantic checking or recover bindings from emitted text.

Ordinary `--systemverilog` emits the production design. Verification
declarations do not add production gates or alter its hardware identity.
Formal execution creates a separate artifact and harness with the observations
needed by the selected route. For supported source contracts,
`--contracts-sva` can additionally publish bindable SVA; this is an output
artifact, not the semantic authority.

For temporal capacity-one sharing, the miter records an accepted input
transaction, computes the exact typed semantic result, and compares it with the
corresponding retired output transaction. It checks occupancy, ordering, no
spontaneous/duplicate output, reset epochs, stalled-output stability, and
same-edge retire/reload. It does not compare transactions by a fixed absolute
cycle number.

## Run verification directly

Check and execute all applicable source safety/cover jobs:

```sh
.venv/bin/zlang examples/verification/bounded_counter.zhl \
  --top BoundedCounter \
  --verify \
  --verify-require proven \
  --formal-depth 16 \
  --formal-timeout 45 \
  --verification-work-dir build/verify-work/counter \
  --verification-report build/verify/counter-result.json \
  --verification-format json
```

Use `--verify-require checked` when bounded checking is sufficient. A `proven`
request runs BMC first and enters the proof stage only after every applicable
safety job passes the bounded stage.

To combine source verification with formal-aware implementation selection:

```sh
.venv/bin/zlang design.zhl --top Top \
  --verify \
  --formal-policy required_bmc \
  --formal-depth 20 \
  --formal-cache build/formal-cache \
  --verification-work-dir build/verify-work/Top \
  --verification-report build/verify/Top.json \
  --verification-format json
```

A non-`off` `--formal-policy` without `--verify` executes selection-time
candidate equivalence only. `--verify` with policy `off` executes source and
automatic safety/cover jobs only. Combining them records both evidence families
without treating one as a substitute for the other.

## Publish and replay a bundle

Publish immutable inputs:

```sh
.venv/bin/zlang design.zhl --top Top \
  --verification-bundle build/verify/Top
```

Replay bounded model checking:

```sh
.venv/bin/zlang verify build/verify/Top \
  --mode bmc \
  --depth 20 \
  --timeout 45 \
  --work-dir build/verify-work/Top-bmc \
  --cache build/verification-cache \
  --report build/verify/Top-bmc.json \
  --format json
```

Replay a proof attempt:

```sh
.venv/bin/zlang verify build/verify/Top \
  --mode prove \
  --depth 20 \
  --timeout 45 \
  --work-dir build/verify-work/Top-prove
```

The bundle stays immutable. Solver configuration, logs, VCD traces, execution
time, and run results belong to the replay report/cache and work directory.

## Result meanings

| Status | Exact meaning |
| --- | --- |
| `bounded_pass` | No counterexample was found through the stated depth. |
| `proven` | The requested unbounded safety proof route completed. |
| `failed` | A reachable counterexample was produced. |
| `witnessed` | A cover condition was reached by one trace. |
| `bounded_unreached` | No cover witness was found within the bound. |
| `unknown` | The route ran but did not establish the requested result. |
| `skipped` | The route was inapplicable or unavailable with an explicit reason. |

Never promote `bounded_pass` to `proven`. Likewise, simulation success,
synthesis acceptance, structural validation, representation invariants, and
estimated or routed Fmax are different evidence categories.

## Evidence identity and caching

Candidate-equivalence evidence is bound to the relation, semantic and selected
implementation identities, Direct-SV artifact hash and bindings, harness,
assumptions, reset contract, BMC depth, and formal tool/configuration identity.
A change to any of those inputs cannot reuse stale decisive evidence.

Verification bundles and run reports preserve exact statuses and route
provenance. A missing observation, unsupported reset/domain, malformed bundle,
tool failure, timeout, or cache mismatch fails closed; it is never normalized
to success.

Machine-readable evidence reports expose the facts owned by the executed
route rather than reconstructing them from report prose. For bounded model
checking this includes the exact status, route, depth, property or relation
identity, solver, recorded tool versions, and `unbounded: false`. Reset
contract and assumption information is included only when it is present in the
compiler-owned verification product; an absent fact is not guessed.

The capacity-one temporal route additionally records relation type
`capacity_one_transaction_stream`, latency 4, II 4, capacity 1, same-edge
retire/reload, and `required_proven_supported: false`. These are properties of
the selected temporal implementation and its proof recipe, not a claim of
unbounded proof. Solver-matrix reports retain one shared immutable problem
identity, a distinct run identity and result for each solver, and explicit
disagreement records.

## Debugging failures

1. Read the source-attributed diagnostic/report before inspecting RTL names.
2. Retain `--verification-work-dir` or replay with `--work-dir`.
3. Inspect the generated `.sby`, logs, and `trace.vcd` inside that directory.
4. Confirm assumptions are feasible and scoped to environment-owned inputs.
5. Confirm the requested depth/status is strong enough for the claim.
6. If a binding or applicability error occurs, minimize the ZLang source and
   report it as a compiler correctness issue rather than hand-editing the
   generated harness.

Use `--format json`/`--verification-format json` for automation. Do not parse
human report prose or private RTL names to recover formal meaning.

## Current boundaries

ZLang does not claim arbitrary temporal logic, liveness/fairness, general
hierarchical equivalence, CDC correctness proof, general HLS equivalence,
modulo scheduling proof, or arbitrary external endpoint compliance. The
capacity-one temporal route is bounded BMC only. Applicability limitations are
part of the result and must remain visible.

See also:

- [Optimization and formal verification](language-reference.md#reference-optimization-formal)
- [Formal examples](../examples/verification/README.md)
- [Toolchain installation](language-reference.md#reference-installing-toolchain)
- [Backend and verification CLI](language-reference.md#reference-installing-toolchain-check-and-run-zlang)
- [Current limitations](language-reference.md#reference-known-limitations)
