# Test strategy

The machine-readable release minimum, allowed skip count, corpus size, and
pinned external-tool versions are recorded in
[`release/status.json`](../release/status.json). The release workflow validates
two complete JUnit reports against that file. Historical per-slice counts remain
evidence for their recorded revisions, not the current release baseline.

ZLang keeps two different kinds of evidence:

1. small independent semantic, negative, mutation, behavioral, and formal
   assertions, which preserve precise failure localization;
2. broad conformance/toolchain gates, which exercise many positive language
   forms in one invocation.

The number printed by pytest is not the main runtime cost. Collection of the
complete suite takes only a few seconds; repeated Clash, Verilator, Yosys/SBY,
and source/top compilation dominate wall time. Tests therefore share immutable
results only inside one module-scoped run and never use a persistent cache that
could hide source, dependency, tool-version, or mutation changes.

## Routine gates

Check all 27 independent tops in the executable language tour through parsing,
semantic analysis, canonical round-trip, and direct artifact construction:

```sh
.venv/bin/python -m pytest -q -m conformance
```

On the acceptance host this is one broad pytest item and completed in 4.64 s.
It is a positive surface check, not a replacement for negative diagnostics or
cycle behavior.

Run the broad positive gate together with exhaustive strict direct-SV lint of
all supported example roots and real Clash/Verilator for every language-tour
top:

```sh
.venv/bin/python -m pytest -n 2 --dist=loadscope -q \
  -m 'conformance or toolchain_smoke'
```

This is three broad pytest items and completed in 75.46 s on the acceptance
host. Genuine tool absence remains an explicit skip.

## Complete acceptance

The complete suite remains required before accepting a compiler slice:

```sh
.venv/bin/python -m pytest -n 8 --dist=loadscope -q
```

It retains unique width/type failures, malformed canonical/artifact tests,
reset/stall traces, formal mutations, proof-status classification, project and
wheel resolution, independent backend ABI checks, and numerical boundary
vectors. These cannot be represented honestly by one valid `all_syntax.zl`
program.

Use the exhaustive external-tool marker when isolating the two corpus-wide
release gates:

```sh
.venv/bin/python -m pytest -n 2 --dist=loadscope -q \
  -m exhaustive_toolchain
```

## Shared language-tour catalog

`tests/conformance/catalog.py` is the only test-owned list of the 27
`examples/all_syntax.zl` tops. Editor tests remain lexical; positive compiler
conformance belongs to `tests/conformance/test_language_tour.py`. The real
Clash gate consumes the same catalog.

The direct-SV example registry discovers every `.zl` source and root
recursively. Within that module, a source/top compilation result is immutable
and cached for the duration of the test process, so artifact/status and strict
lint checks do not compile the same root twice. The cache is discarded at
process exit and mutations always work on test-local output files.

Measured before/after for the combined example status plus strict-lint gate:

| Version | Result | Wall time |
|---|---:|---:|
| before result reuse | 2 passed | 141.90 s |
| after result reuse | 2 passed | 89.23 s |

The 37% reduction comes from removing repeated compiler work, not from hiding
or deleting assertions.
