# ZLang HDL Community Baseline

Release designation: **2026-09 public release**

Compiler license: **Apache-2.0**, with the existing file/subtree exceptions in
[REUSE.toml](../../REUSE.toml) and [NOTICE](../../NOTICE) retained.

Policy approved: 2026-09-08. The release designation is a target, not a claim
that a release/tag has already been published. The exact public commit, signed
tag and source/artifact hashes must be recorded in the final release evidence.

## Baseline rule

Everything in this release remains Community.

Enterprise functionality begins with functionality developed after this
baseline unless explicitly classified otherwise. This applies to **new
functionality**, not retrospective reclassification of baseline capabilities.
New additions receive an explicit Community or Enterprise classification before
implementation; development date alone does not assign a software license.
Bug fixes and compatibility repairs to Community capabilities remain Community.

The baseline includes the complete accepted compiler and its current local
workflow: language semantics, both backends, simulation, CSR hardware and
JSON/Markdown views, formal verification, equivalence, proof caching and bundle
replay, e-graphs, architecture/pipeline exploration, source maps, standard
libraries, tooling and release fixtures. It also includes the existing Verilator
VPI C++ state-access header generator. No baseline capability is removed to
create an Enterprise dependency.

Community is an edition classification, not a replacement license. In particular,
the Wi-Fi reference project stays MIT and the code-of-conduct exception stays
CC-BY-4.0. Their inclusion in Community does not relicense them as Apache-2.0.

## Explicit post-baseline Enterprise classifications

The maintainer has classified these **future, not-yet-implemented generators**
as Enterprise:

- CSR C/C++ software helper generation, such as register-map headers and typed
  host-side register access helpers.
- SystemVerilog UVM helper generation, such as UVM register-model and integration
  helpers.

Existing CSR semantics, RTL generation, JSON/Markdown, formal checks, direct-SV
output and Verilator integration remain Community. Generating C++ for simulation
is not the same feature as generating a CSR host-software API. Existing SVA
contracts and formal harnesses are not UVM helpers.

Other new capabilities require their own classification; this document does not
automatically reserve every future enhancement for Enterprise. It also does not
restrict independent users from building compatible tools under the applicable
licenses. This is the boundary for project-provided editions.

## Implementation and licensing boundary

An Enterprise generator may consume Community's public typed CSR model and
versioned artifacts; Community must not import or require the Enterprise
package. Shared correctness fixes remain in Community. Do not duplicate CSR
semantics, add licensing gates to baseline features, or put Enterprise sources
under the current broad `zlang*` package selection.

No Enterprise package, entitlement mechanism or commercial license is provided
by this policy. Final Enterprise terms, contribution rights, and licenses for
generated C/C++/UVM helper material require separate approval before distribution.
The compiler license is not automatically attached to user designs or generated
files; the existing qualified [generated-output policy](../../NOTICE) remains.

Previous Apache grants and third-party obligations remain intact. This policy
does not alter [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0), transfer
copyright or claim exclusive rights over previously licensed code.

See [editions](../editions.md), [project scope](../project-scope.md), and
[release requirements](../../RELEASING.md).
