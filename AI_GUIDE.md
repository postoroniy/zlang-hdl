# AI guide for ZLang HDL

This is the vendor-neutral entry point for coding assistants working with
ZLang HDL. Qwen can additionally discover
[the repository skill](.qwen/skills/zlang-hdl/SKILL.md), but that skill is a
compact router over the same compiler-owned documentation. Do not create a
second language specification for a particular model.

## Sources of truth

Use this order when sources disagree:

1. the user's request and the nearest applicable `AGENTS.md`;
2. grammar, typed semantic analysis, backend-independent IR, validators, and
   passing tests in the current checkout;
3. `zlang/public_capabilities.py` and the current language status;
4. the language quick reference and topic guides;
5. compiling examples and source-authored `stdlib/**/*.zhl`;
6. editor grammar only for lexical highlighting.

Historical design-freeze documents explain past decisions; they do not
override the current compiler. Generated SystemVerilog is an implementation
artifact, not the definition of ZLang semantics.

## Work in the checkout's environment

Preserve all existing tracked and untracked work. Use the checkout-local
environment and scratch policy:

```sh
git status --short
make venv
source .venv/bin/activate
make env-check
```

Run repository tools through `.venv/bin/...` or Make targets. Keep temporary
work below `build/tmp`; `KEEP_TMP=1 make ...` retains failure evidence. Do not
borrow a sibling checkout's virtual environment.

## Essential language rules

- `=` is a current-cycle combinational drive. `<-` is an atomic next-edge
  update of stored state.
- `out name : T` is a wire. `out reg name : T = RESET` is stored output state.
  A rule or FSM uses `drive name = value` for a transient wire output.
- Width, signedness, fixed-point scale, quantization, reset, clock domain,
  latency, initiation interval, and protocol ownership are exact. Never insert
  an implicit conversion, CDC, or protocol adapter.
- Runtime control uses expressions, `when`, `priority`, or `fsm`. Compile-time
  `if` elaborates structure; do not invent procedural software syntax.
- `implement { expression intent { ... } }` selects among bounded exact-value
  and physical implementation candidates. Egglog owns pure value equivalence;
  scheduling, resource binding, ready/valid flow, and formal evidence have
  separate owners.
- Ordinary scalar `implement` remains II=1. The only II>1 sharing surface is
  the documented capacity-one ready/valid `a*b+c*d` candidate.
- Direct SystemVerilog is the only production RTL backend.

Start with [the concise language reference](docs/language-quick-reference.md).
Read [formal verification](docs/formal-verification.md) before changing or
claiming verification behavior.

## Evidence before claims

Use the smallest relevant sequence:

```sh
.venv/bin/zlang design.zhl --top Top --check
.venv/bin/zlang design.zhl --top Top \
  --systemverilog build/Top.sv --verilator-lint
.venv/bin/python -m pytest -q path/to/focused_test.py
make static
```

When formal verification is relevant, report the exact route and status.
`bounded_pass` is not `proven`; synthesis is not formal verification; estimated
Fmax is not timing closure. Missing tools, bindings, or unsupported reset
semantics are explicit unavailable evidence, never success.

## Compiler changes

Keep ownership explicit:

```text
immutable models own facts
stateful services own caches/indexes/state
passes own bounded transformations
domain modules own semantics
```

Do not introduce a utility dumping ground, universal context, generic graph,
deep pass hierarchy, or a new god module/class. A shared abstraction must own a
clear invariant and serve multiple real consumers. Preserve semantic,
implementation, artifact, source-map, simulation-plan, and formal identities
unless the task explicitly changes their schema.

For formal work, preserve the pipeline described in
[formal verification](docs/formal-verification.md): source predicates and
semantic-reference relations are bound through compiler-published
`BackendArtifact` bindings to separate formal RTL/harnesses. Never recover
meaning by parsing generated RTL names.

## Handoff

State only evidence that actually ran:

- exact source and top;
- behavior or invariant changed;
- files and authoritative owner changed;
- commands, tools, counts, and statuses;
- identity/RTL changes, if any;
- first remaining blocker and unsupported adjacent features.

Do not commit, push, tag, publish, or update release artifacts unless the user
explicitly requested that external action.
