# Licensing and design provenance

ZLang HDL's Community compiler is distributed under
[Apache-2.0](../../LICENSE). Individual files and subtrees retain the licenses
recorded in [REUSE.toml](../../REUSE.toml) and [NOTICE](../../NOTICE), including
the MIT Wi-Fi reference project and the CC-BY-4.0 code of conduct.

The edition name does not determine ownership of a hardware design or replace
the license applying to its source.

- [Generated output](GENERATED_OUTPUT_POLICY.md): user designs, included
  libraries, helper bodies, ROM images and external-tool output are distinct
  provenance cases.
- [IP classification](IP_CLASSIFICATION.md): the roles and recorded licenses of
  examples, regression fixtures and reference designs.
- [Community baseline](COMMUNITY_BASELINE.md): the identified release snapshot
  and production-backend policy.
- [Community edition](../editions.md): the repository's distributed edition.
- [Contribution policy](../../CONTRIBUTING.md): DCO and third-party attribution.
- [Branding](../../TRADEMARKS.md): project naming, separate from software rights.

## What each distribution includes

The public source tree includes compiler sources, standard libraries, examples,
tests and their applicable license/notice files. The Python wheel and sdist
contain the compiler and shipped standard libraries, with the root LICENSE and
NOTICE; they do not include the Wi-Fi example project or the repository test
suite. External synthesis, simulator and solver installations are not bundled
into those Python distributions.

Dependency declarations, REUSE checks, artifact hashes and SBOMs support release
review. They do not independently establish authorship, patent clearance or
permission to redistribute material imported from another project. External
toolchains are not compiler, package, CI, or release dependencies unless a
corresponding validation flow is requested.

These documents explain project policy and the inspected distribution paths.
They do not change any license, grant rights over third-party material, or
provide a legal opinion on a particular hardware product. Questions about
ownership, copied implementation material or additional commercial terms need
review before that material is distributed.
