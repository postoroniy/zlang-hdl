# Community public release checklist

This checklist defines the evidence required for the **2026-09 Community
Baseline**. A policy approval is not a published tag or a passed CI run. Record
the actual public commit, source projection identity, signed tag and archive
hashes in the release record after the corresponding checks complete.

## License and edition boundary

- Preserve the unmodified root Apache-2.0 license and existing MIT/CC-BY-4.0
  exceptions, their notices and contribution rules.
- Retain every baseline compiler capability and all accepted correctness fixes.
  The new CSR C/C++ and UVM generators are prospective Enterprise features,
  not removed Community functionality or available products.
- Review contributor and external-source provenance, dependency inventory and
  SBOM. Do not treat DCO or REUSE metadata alone as proof of all IP rights.
- Review [generated output](GENERATED_OUTPUT_POLICY.md) and
  [IP classification](IP_CLASSIFICATION.md), including copied helper/template
  material, ROM contents and external vendor models.
- Use accurate project naming without asserting trademark registration.

## Snapshot and package isolation

- Preserve public Git ancestry and signed-off contributions; reconcile changes
  from public main before exporting the complete accepted compiler snapshot.
- Validate the public allowlist, complete source/test/stdlib/fixture closure,
  deterministic manifest, links and source imports.
- Exclude private development history, coordination files, machine paths,
  credentials, scratch results and unreviewed design inputs.
- Inspect source, history, wheel, sdist, documentation, workflow logs and
  downloadable artifacts for secrets and unintended private material.
- Run REUSE and dependency/security checks on the final snapshot. Ensure the
  runtime distributions carry LICENSE/NOTICE and all packaged stdlib sources;
  the source distribution's actual contents determine any additional notices.
- Exercise installed CLIs and verification-bundle replay outside the checkout;
  no private package, Enterprise service or commercial EDA installation may be
  required for ordinary Community installation.

## Validation and publication

- Run two full eight-worker no-skip regressions on the unchanged release
  candidate, retaining JUnit summaries and tool versions. Do not reuse historical
  test counts as evidence for different source.
- Run direct-SV/Verilator and the existing Yosys/SBY/Z3 checks,
  compileall, static correctness, package validation and diff checks.
- Verify hosted checks, DCO and the trusted main/tag-only EDA runner. Never
  execute untrusted pull-request source on the dedicated runner.
- Check repository visibility effects before reopening it: source/history,
  Actions logs and artifacts may become public. Confirm branch/tag rules and
  required checks after the visibility change.
- Promote through a reviewed public pull request, never by pushing private
  history or force-updating main. Preserve the existing PR-only admin exception
  without using it to excuse a failing check.
- Publish a release/tag only after the signed-tag and artifact gates in
  [RELEASING.md](../../RELEASING.md) pass. Do not imply an alpha release is a
  general production/safety certification.

See [Community Baseline](COMMUNITY_BASELINE.md) and the existing
[public-snapshot publication process](../../release/PUBLICATION.md).
