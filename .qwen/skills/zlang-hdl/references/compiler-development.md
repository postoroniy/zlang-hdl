# Compiler development workflow

Use this reference for Python compiler, IR, backend, CLI, tooling API, package,
or test changes.

## Preserve layer ownership

Trace one minimal program through:

```text
lexer/parser AST
  -> typed semantic IR
  -> canonical/high-level IR
  -> optimization and implementation planning
  -> selected scheduled/resource IR
  -> direct-SystemVerilog emission and BackendArtifact
```

Fix the first layer that loses or corrupts meaning. Do not parse generated RTL,
recover semantics from strings, dispatch on application/module/member names, or
add a second scheduler/type codec/resource matcher inside a backend.

Before a compiler fix:

1. Save a minimal standalone `.zhl` reproducer and exact diagnostic.
2. Confirm expected support in current docs/capabilities/tests.
3. Identify parser, semantic, canonical, simulator, optimizer/scheduler,
   resource planner, direct-SV, formal, resolver, or environment ownership.
4. Add a focused negative or mutation test that fails for the real defect.
5. Implement the smallest reusable fix and validate malformed IR/restoration
   paths when an IR contract changes.

Maintain deterministic semantic identities. Source locations and measured/tool
metadata must not contaminate value identities unless the schema explicitly
requires it. Never use Python object IDs or guessed RTL names in persistent
records.

Direct SystemVerilog consumes already selected typed/scheduled/resource IR. It
must not rediscover arithmetic architecture. Publication is fail-closed: missing
or duplicate feature inventory, bindings, domains, storage ownership, or
unsupported nodes prevent BackendArtifact publication.

## Tests

Run the smallest focused suite first, then the active plan's gates. Current broad
commands are:

```sh
.venv/bin/python -m pytest -q -m conformance
.venv/bin/python -m pytest -n 2 --dist=loadscope -q \
  -m 'conformance or toolchain_smoke'
.venv/bin/python -m pytest -n 8 --dist=loadscope -q
.venv/bin/python -m compileall -q zlang tests
git diff --check
git diff --cached --check
```

The complete regression cannot honestly be replaced by one `all_syntax.zhl`
compile: negative diagnostics, mutations, reset/stall traces, proof-status
classification, packaging, and numerical boundaries need independent tests.

Do not add a skip to hide a regression, treat missing production tools as
success, or claim release acceptance from a focused
suite. Preserve unrelated dirty/untracked work and generated artifacts outside
tracked source.

Read the testing, backend, and tooling chapters in
`docs/language-reference.md` when those surfaces are touched.
