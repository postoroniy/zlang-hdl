---
name: zlang-hdl
description: Implement, review, debug, compile, optimize, and verify ZLang HDL projects and compiler changes. Use for .zhl source, stdlib, direct SystemVerilog, formal verification, implementation intent, DSP/pipeline planning, or standards-based hardware conversion. Do not use this skill to invent syntax, revive Clash, or treat estimates and bounded checks as proof.
---

# ZLang HDL

Use the checked-out compiler as executable authority. ZLang is an exact typed
hardware language. Its semantics come from backend-independent typed IR, not
from generated RTL or a historical design document. Direct SystemVerilog is the
only production backend; Clash and executable M38 are retired.

The productive loop is:

```text
bounded hardware contract
  -> repository context
  -> closest supported ZLang pattern
  -> smallest source/compiler change
  -> semantic, RTL, oracle, and applicable formal evidence
  -> one completed slice or one minimized blocker
```

## Establish the workspace

Start at the Git root and preserve all existing changes:

```sh
git rev-parse --show-toplevel
git status --short
git rev-parse --short HEAD
```

Read the applicable `AGENTS.md` before acting. Never assume a clean tree and
never discard, overwrite, stage, commit, or publish unrelated work.

## Authority and documentation routing

Use this order:

1. the user's current request and applicable `AGENTS.md`;
2. grammar, typed semantics, validators, and passing tests in this checkout;
3. `zlang/public_capabilities.py` and `docs/syntax-support-matrix.md`;
4. `docs/language-quick-reference.md` and the relevant topic guide;
5. compiling examples and source-authored `stdlib/**/*.zhl`;
6. editor grammar only for lexical behavior.

`docs/language-guide.md` is an index, not a root-level file. Do not create a
duplicate. `examples/all_syntax.zhl` is a representative language tour, not an
exhaustive capability contract. Historical milestone/design-freeze prose does
not override current executable behavior.

Read only the reference relevant to the task:

- Source authoring, state, storage, hierarchy, protocols, numeric rules, and
  production commands: [references/language-and-rtl.md](references/language-and-rtl.md).
- `implement`, exact pipelines, egglog, target/resource planning, and formal:
  [references/optimization-and-formal.md](references/optimization-and-formal.md).
- Parser/semantic/IR/backend/tooling changes and regression discipline:
  [references/compiler-development.md](references/compiler-development.md).
- IEEE/bus/crypto/DSP conversion:
  [references/standards-conversion.md](references/standards-conversion.md).

Search for one construct and one close executable witness instead of loading the
entire repository:

```sh
rg -n "FEATURE_OR_TYPE" examples stdlib docs tests zlang
```

## Choose the operating mode

- **Source/design slice:** preserve the stated interface, widths, domains/reset,
  latency/II, backpressure, storage, numeric policy, and oracle. Do not change
  the compiler merely to avoid a valid diagnostic.
- **Compiler defect:** preserve a minimal non-application `.zhl` reproducer,
  confirm documented support, and fix the lowest shared layer. Never add a
  Wi-Fi/FFT/bus/name special case.
- **Capability review:** inspect and report evidence without mutation unless the
  user also requested implementation.
- **Standards conversion:** separate authoritative facts, local architecture
  choices, and unknowns before writing hardware.
Keep one bounded active slice. Do not expand into adjacent features unless the
user explicitly asked to continue until the first blocker.

## Required evidence loop

1. Identify the exact source/top and closest passing witness.
2. Record the expected observable behavior and unsupported boundaries.
3. Make the smallest general change.
4. Run semantic checking immediately.
5. Add or update a focused negative/behavioral/oracle test.
6. Emit direct SystemVerilog and validate it when backend behavior is claimed.
7. Run applicable existing formal routes without broadening their semantics.
8. Run the active task's focused/full gates and diff checks.

Use the repository environment and explicit top:

```sh
.venv/bin/zlang source.zhl --top ExactTop --check --verbose
.venv/bin/zlang source.zhl --top ExactTop \
  --systemverilog build/ExactTop.sv --verilator-lint --verbose
```

The command is `zlang`, not `zlangc`. There is no supported Clash/Haskell output
path. A backend failure or missing tool is never a successful result.

## Non-negotiable semantic guardrails

- `=` is a current-cycle drive; `<-` is an atomic next-edge state update.
- Types, widths, signedness, fixed scale, reset behavior, latency, II, protocol
  ownership, and clock domains are exact.
- Do not invent `let`, `const`, `var`, `return`, semicolons, runtime procedural
  `if`, implicit casts, protocol adapters, or CDC.
- Compile-time `if` elaborates. Runtime values use `?:`/`mux`/`switch`; runtime
  effects use `when`/`else when`/`else`, `priority`, or `fsm`.
- `implement { expression intent { ... } }` is the scalar compiler-selection
  surface. `pipeline(N)` is exact N-cycle hardware. Scalar `pipeline(auto)`,
  `architecture(auto)`, and `explore` are retired. Protocol
  `transform pipeline(auto, ...)` is a separate bounded ready/valid feature.
- Egglog proposes exact pure-value alternatives; it never places registers or
  chooses DSP primitives. Scheduling and resource matching are separate.
- Never move fixed-point quantization, silently resize, guess physical RTL names,
  or weaken fail-closed publication to make a design compile.

## Completion handoff

Report only executed evidence:

```text
Status: DONE | BLOCKED
Slice: exact source/top and observable contract
Changed: files and behavior actually changed
Validation: commands, counts, tools, and exact statuses
First blocker: owning layer and diagnostic
Minimal reproducer: path and command
Not claimed: adjacent features, timing, proof, compliance, or untested routes
```

Never finish with “should compile”, “should synthesize”, “proved”, “meets Fmax”,
or “standards compliant” without the matching executed evidence. If this skill
conflicts with current compiler behavior, follow the repository, record the
drift, and update the skill only after confirming a general rule.
