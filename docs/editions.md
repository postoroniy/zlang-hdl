# ZLang HDL Community edition

This repository publishes the ZLang HDL Community compiler and its documented
tooling under Apache-2.0, with the exceptions recorded in
[REUSE.toml](../REUSE.toml) and [NOTICE](../NOTICE).

The [Community baseline](licensing/COMMUNITY_BASELINE.md) identifies the
released snapshot and its production-backend policy. The capability registry,
language reference and executable release tests define the exact supported
surface; edition wording does not extend those technical claims.

No additional product edition, entitlement mechanism, restricted compiler
feature or service dependency is required to use the functionality in this
repository.

The optional Linux x86-64/WSL2 native-simulation wheel is a separately audited
binary accelerator whose Rust/Cranelift source is not distributed here. The
Community compiler and reference simulator remain usable without it; see the
[native simulation section](language-reference.md#reference-native-simulation).
