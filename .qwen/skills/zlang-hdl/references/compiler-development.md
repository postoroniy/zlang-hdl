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

Prepare this checkout with `make venv PYTHON_PROFILE=dev` before running tests;
it includes the complete test/formal dependencies and Ruff. Use `make venv`
for release/audit work, which needs the default `release` toolset. Ordinary
`make install` uses the smaller runtime profile. Profiles add dependencies and
never prune the environment; `.[formal]` alone does not install pytest or the
release tools. See [dependency profiles](../../../../docs/installing-toolchain.md#reference-installing-toolchain-python-dependency-profiles).

Run the smallest focused suite first, then the active plan's gates. Use the
repository-owned CI lanes for complete deterministic and isolated performance
validation. Each lane preserves its release-status, tool and no-skip checks.

```sh
make static
make test-fast
make ci-full-regression WORKERS=4 CI_JUNIT=build/ci-full.xml
make ci-performance-regression CI_JUNIT=build/ci-performance.xml
make ci-test-floor \
  CI_DETERMINISTIC_JUNIT=build/ci-full.xml \
  CI_PERFORMANCE_JUNIT=build/ci-performance.xml
git diff --check
```

Run the performance lane after deterministic workers finish. Its wall/RSS
ceilings require an otherwise idle host. Keep both JUnit reports for the
combined release-floor check.

The complete regression cannot honestly be replaced by one `all_syntax.zhl`
compile: negative diagnostics, mutations, reset/stall traces, proof-status
classification, packaging, and numerical boundaries need independent tests.

Do not add a skip to hide a regression, treat missing production tools as
success, run retired Clash tests, or claim release acceptance from a focused
suite. Preserve unrelated dirty/untracked work and generated artifacts outside
tracked source.

Use the [contribution workflow](../../../../CONTRIBUTING.md),
[release procedure](../../../../docs/release-process.md), current Make targets
and focused tests for the affected owner. These references exist in the public
distribution as well as the integration checkout.

For documentation changes, distinguish maintained guides from explicitly dated
evidence. Check tracked Markdown links, but preserve historical versions,
measurements and tool results. Current release/tool claims must come from
`release/status.json`; a checked-in candidate identity is not a publication
claim. Do not copy source or claims from a broader checkout without checking
this distribution's executable support and projection boundary.
