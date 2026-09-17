# ZLang HDL Community Baseline

The published Community baseline is **ZLang HDL v0.1.0a9**, released on
2026-09-17 from commit
[`0ba77eaaef425b69bb368eea6f56faab856566e2`](https://github.com/postoroniy/zlang-hdl/commit/0ba77eaaef425b69bb368eea6f56faab856566e2).
The corresponding signed tag is
[`v0.1.0a9`](https://github.com/postoroniy/zlang-hdl/releases/tag/v0.1.0a9).

The compiler and the repository are licensed under Apache-2.0, subject to the
file and subtree exceptions recorded in [REUSE.toml](../../REUSE.toml) and
[NOTICE](../../NOTICE). Community is suitable for commercial and
non-commercial hardware development under those applicable licenses.

## Production backend policy

Direct SystemVerilog is the sole supported production RTL backend. ZLang
semantics are defined by backend-independent typed IR and compiler-owned
semantic and timing models.

Clash was a pre-baseline experimental backend. It was retired before this
baseline and is not part of the Community package, command-line interface,
release acceptance, or compatibility contract.

## Included surface

The baseline records the language, compiler, standard library, simulator,
direct-SystemVerilog generation, local verification, implementation planning,
source maps, build artifacts, examples, tests and editor integration shipped in
the tagged release. The machine-readable capability registry and release tests
are authoritative for the exact supported combinations.

This document records an identifiable release; it is not a promise that every
experimental alpha API remains unchanged. Compatibility and migration are
governed by the release notes, diagnostics and versioned artifact schemas.

Separately licensed reference material retains its recorded license. Inclusion
in the Community repository does not relicense that material as Apache-2.0.
