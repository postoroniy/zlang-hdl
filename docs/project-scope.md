# Open-source project scope

ZLang HDL is an open-source hardware language and compiler distributed under
the Apache License 2.0. Files or subtrees identified by REUSE metadata and
adjacent notices retain their stated compatible licenses. This document records
the boundary between the Community compiler and future Enterprise additions;
it does not change those licenses. The approved
[2026-09 Community Baseline](licensing/COMMUNITY_BASELINE.md) retains everything
in that release, including all existing local verification and exploration.

## Open-source core

The complete language and local implementation workflow remain part of the
open-source project. That scope includes:

- language syntax, type and cycle semantics, and compiler-owned standard
  libraries;
- parsing, semantic analysis, canonical and selected IR, optimization, and
  source mapping;
- production direct-SystemVerilog emission, simulation, manifests, and local
  build tooling; the retired Clash compatibility implementation remains
  Community source but is not a supported compiler path;
- local safety verification (M35), semantic-reference equivalence (M36),
  historical M38 records, formal-aware candidate selection (M39), and
  immutable verification bundles;
- local implementation selection, including `implement`, `choice`, cost
  extraction, and local synthesis
  evidence; and
- reproducible projects, logical imports, dependency locking, implementation
  profiles, and artifact identities.

Baseline compiler, verification, exploration, and backend capabilities are not
reserved for a separate commercial edition. Their correctness and compatibility
repairs remain Community. Apache-2.0 distributions may be used, modified, and
redistributed under the terms of that license.

## Post-baseline Enterprise additions

New functionality developed after the baseline may be classified as Enterprise
before implementation. The maintainer has specifically classified future **CSR
C/C++ software helper generation** and **SystemVerilog UVM helper generation**
as Enterprise. These generators are not implemented or offered yet.

Existing CSR hardware/RTL, JSON/Markdown exports, SVA/formal harnesses and the
Verilator C++ state-access generator remain Community. Enterprise generators
may consume Community's public typed models/artifacts; Community must not
depend on Enterprise. Final commercial and generated-helper terms require
separate review. See the [edition matrix](editions.md).

## Possible services above the core

Separate hosted or commercial offerings may provide operational capabilities
above the open-source tools, for example:

- managed agents and workflow automation;
- distributed exploration and synthesis farms;
- managed multi-vendor tool orchestration and evidence databases;
- private package or IP registries and organization access controls;
- artifact signing, provenance services, and policy enforcement;
- collaboration, enterprise integrations, on-premises deployment, and support.

Such services are infrastructure and operations around the open-source
language/compiler boundary, alongside the separately classified new generators.
Neither category removes baseline language, backend, local verification or
local exploration capabilities. This model does not depend on retroactive
proprietary relicensing of community contributions. Community's existing DCO
contribution policy remains unchanged and does not require a Contributor
License Agreement; future Enterprise contribution terms are a separate review.

## Automation and agents

ZLang's typed intermediate representations, deterministic identities,
structured diagnostics, manifests, and evidence reports provide machine-readable
interfaces that automation and software agents can consume. This is an
architectural property, not a performance or productivity claim; all generated
changes remain subject to the same review, validation, and provenance rules as
human-authored changes.

## Dependencies and future distribution

Source imports remain logical, and reproducibility comes from locked content
and dependency identities rather than a particular hosting service. A future
resolver or registry may populate the existing lock model, but ordinary
compilation remains offline and the locked content identity remains
authoritative. See [Projects and dependencies](projects-dependencies.md).

## Evaluation

A future comparison of agent-authored ZLang HDL and SystemVerilog should publish
the source tasks, model and tool versions, prompts, constraints, validation
criteria, failures, and required human corrections. The project currently makes
no claim that either language is more productive, correct, or efficient for
agent-generated hardware.

This scope statement is not a promise that any listed service will be offered,
nor does it change the support status of a feature. Current executable support
is documented by the capability registry, tests, and
[known limitations](known-limitations.md).
