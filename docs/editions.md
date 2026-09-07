# ZLang HDL editions

The **2026-09 Community Baseline** retains the complete compiler functionality
included in that release. Community is suitable for commercial as well as
non-commercial hardware development under its applicable licenses; it is not
a hobby-only or deliberately restricted edition.

The [baseline policy](licensing/COMMUNITY_BASELINE.md) is approved. The exact
release snapshot/tag is still subject to release acceptance and publication.

| Capability | Edition / status |
|---|---|
| Baseline language, stdlib, simulator, Clash and direct-SystemVerilog | Community; existing capabilities retained |
| Local formal verification, equivalence, caches, bundles and exploration | Community; existing capabilities retained |
| CSR hardware behavior, RTL, JSON and Markdown | Community; existing capabilities retained |
| Verilator state-access C++ header and simulation tooling | Community; existing capabilities retained |
| New CSR C/C++ software helper generator | Enterprise; classified, not implemented |
| New SystemVerilog UVM helper generator | Enterprise; classified, not implemented |
| New managed/distributed workflows, organizational policy and services | Potential future Enterprise additions; individually scoped |

Enterprise can add functionality developed after the baseline. Later work is
not automatically Enterprise: each new feature is classified before
implementation, while fixes to Community functionality stay Community.

Future Enterprise generators can use the public CSR schema and artifacts.
Community compilation, local verification and existing exports must work without
an Enterprise installation or service. No Enterprise download, CLI command,
pricing, license or availability is announced here.

The compiler remains Apache-2.0; separately identified files retain their own
licenses, including the MIT Wi-Fi reference project. Design/IP licensing and
generated-output obligations are separate from the edition name. See
[NOTICE](../NOTICE) and [contribution policy](../CONTRIBUTING.md).
