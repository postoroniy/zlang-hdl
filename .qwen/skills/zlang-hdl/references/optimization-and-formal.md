# Optimization, scheduling, targets, and formal evidence

Use this reference when changing `implement`, `pipeline(N)`, egglog, target or
resource matching, DSP mapping, candidate selection, verification, or reports.

## Responsibility split

The current production flow is:

```text
typed value IR
  -> bounded egglog exact-value alternatives
  -> typed computation DAG
  -> generic/resource covering
  -> target-aware exact-N scheduler
  -> deterministic candidate extraction
  -> optional formal-aware selection/semantic-reference equivalence gate
  -> direct SystemVerilog
```

Egglog handles pure zero-latency value equality only. It never places registers,
models protocols/state, or names DSP48 registers. The pipeline scheduler owns
stage assignment and balancing; the resource matcher owns generic versus target
resource coverage. Unsupported nodes and semantic barriers fail closed.

The current shared optimizer contracts live in `zlang/opt/` and the resource
matcher in `zlang/resource_matching.py`. Do not recreate local type codecs,
rewrite tables, cost vocabularies, or resource compatibility checks in a
planner/backend.

## Source policy

Use `implement` for compiler-selected scalar alternatives:

```zlang
y = implement {
    dot(a, b)
    intent {
        latency <= 4
        ii == 1
        dsp <= 8
        fmax >= 100
        minimize lut
    }
}
```

The expression fixes the value; `intent` adds hard constraints and one objective.
An estimate is not timing closure. `pipeline(N) { expr }` is exact N-cycle
hardware and must remain exactly N. `choice` is for user-authored alternatives.
Scalar `pipeline(auto)`, `architecture(auto)`, and `explore` are retired.
Protocol `transform pipeline(auto, ...)` remains a distinct globally stalled
ready/valid construct.

Target-aware scheduling may bind legal operations to current resource models
(including the bounded Xilinx 7-Series DSP48E1 path) and leave the rest in
fabric. Do not embed vendor names in egglog or generic typed value IR. Preserve
exact widths, signedness, fixed scale, quantization barriers, reset/enable
semantics, latency, II, shared DAG nodes, and reconvergent alignment.

## Formal model

- safety verification/source goals cover existing bindable safety properties and bounded cover.
- semantic-reference equivalence compares a selected implementation against the compiler-owned semantic
  reference with exact timing windows where supported.
- formal-aware selection gates candidate selection according to its explicit policy.

Source supports named same-cycle `assert`/`ensure`, scoped `contract`/`require`,
bounded `cover`, and legacy `assume`/`guarantee`. Do not invent temporal syntax,
liveness, fairness, arbitrary SVA/SMT, new observation families, or source-level
semantic-reference equivalence controls.

```sh
.venv/bin/zlang design.zhl --top Top --verify \
  --verification-report build/verify.json --verification-format json
.venv/bin/zlang design.zhl --top Top \
  --verification-bundle build/verify
.venv/bin/zlang-verify build/verify --mode bmc --depth 20 \
  --report build/replay.json
```

Use exact status terms:

- `failed`: counterexample exists;
- `bounded_pass`: no counterexample only through the stated depth;
- `proven`: completed proof route;
- `witnessed`: cover reached;
- `bounded_unreached`: cover missed within the stated bound;
- `unknown`/`skipped`: unresolved or unavailable with an explicit reason.

Never relabel BMC, representation invariants, compiler structural validation,
estimated Fmax, or synthesis success as stronger evidence. Verification does not
legalize unsafe hardware or feed range inference.

Read the optimization and formal chapters in `docs/language-reference.md` and the relevant
`examples/verification/` witness before changing these paths.
