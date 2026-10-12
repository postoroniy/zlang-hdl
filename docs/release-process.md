# Public release process

ZLang HDL releases are produced from the exact protected `main` commit in this
repository. The repository is self-contained: release validation does not
depend on another checkout, an unpublished source tree, or a developer's
ambient Python installation.

## Release invariants

- The package version, native wheel version, changelog section, regression
  ledger and `release/status.json` must agree.
- Every release command uses this checkout's `.venv` and collision-safe scratch
  space below `build/tmp`.
- Direct SystemVerilog, native simulation, formal evidence, the VSIX, the PDF
  and package artifacts are validated from one exact Git tree.
- A bounded result is never reported as an unbounded proof, and an unavailable
  check is never reported as success.
- Release workflows validate and retain evidence before any publication step.
- Existing signed tags are immutable.

## 1. Prepare a review branch

Create a normal branch from current public `main`. Update source, tests,
documentation and release metadata together. Do not build release artifacts
from uncommitted changes.

Bootstrap the checkout-local environment and run the fast gate:

```bash
make venv
source .venv/bin/activate
make release-sanity
```

`release-sanity` validates release metadata, regression selectors, the reviewed
PDF, static checks, workflow contracts and editor security evidence before the
long regressions begin. For releases after the bootstrap snapshot, preflight
also discovers the nearest signed release tag and rejects dependency-floor
downgrades.

## 2. Validate the review commit

Public review commits require a cryptographic signature and an exact DCO
`Signed-off-by` trailer. Once the branch is clean and committed, run:

```bash
make release-review
```

This gate runs the fast checks and the complete zero-unexpected-skip regression
twice from fresh source archives. It does not create a tag, push, publish or
modify another checkout.

## 3. Merge and run the non-publishing candidate

After protected-main checks pass, merge the review branch. Dispatch the
`Release` workflow on the exact `main` commit with only the prospective tag.
The workflow discovers any prior release tag from Git history; the initial
one-commit bootstrap has no prior-release input. Candidate mode must finish
with:

- validation successful;
- EDA successful;
- publication skipped;
- no tag or GitHub Release created.

Inspect the retained test, EDA, dependency, native-wheel, VSIX, SBOM and
preflight evidence. A matching hash proves byte identity, not correctness by
itself.

## 4. Publish

Only after the candidate gate is green, create one signed annotated tag at the
accepted `main` commit and push that tag. The tag-triggered workflow repeats
validation, verifies the tag signature and exact-main identity, and is the only
workflow path permitted to publish a GitHub prerelease.

Never rewrite a release tag. If a tagged workflow fails, correct the problem in
a new reviewed commit and prepare a later alpha.

## Artifact requirements

### Native runtime

The public repository carries the audited platform wheel consumed by the
compiler. `make native-release-set`, `make jit-check`, `make jit-audit` and the
hosted vulnerability scan validate its version, ABI, binary inventory, embedded
SBOM and advisory evidence. The package must fail closed when its runtime ABI
does not match the compiler.

### Language reference PDF

The checked-in PDF is a reviewed release artifact. Validation binds its hash,
source hashes, builder identity, cover image geometry, page count and page
geometry through `release/status.json`. Reproducibility checks do not replace
the mandatory all-page visual review.

### VS Code extension

The extension is built from its exact lockfile, audited for current advisories,
packaged into a fresh path and tested in the pinned stable VS Code host. The
extension version may differ from the compiler alpha version; the status file
records the accepted identities.

## Durable regression requirements

Every shipped correctness fix has a stable entry in
`release/regressions.json`, permanent source/test paths and an exact selector.
Pure refactors do not receive artificial regression identities. Release
preflight checks every included selector and binds the ledger digest into its
manifest.

Tests must not rely on an untracked source, mutable global temporary directory,
previously installed ZLang package, or an unpinned EDA tool. Tool-dependent
release tests run with the versions recorded in `release/status.json`.

## Recovery rules

- Run pre-merge work with `make release-review`; exact-main candidate checks are
  intentionally post-merge.
- If source or metadata changes after an artifact build, discard and rebuild
  the wheel, PDF, VSIX, checksums, SBOM and candidate evidence.
- If an Actions job differs from local behavior, reproduce the repository-owned
  Make target rather than adding a second command implementation to workflow
  YAML.
- If an external scanner exits nonzero, parse and validate its complete report;
  do not normalize its status into success.
- Keep failure evidence only in ignored `build/` paths and never include it in
  packages or commits.

## Local command summary

```bash
make venv
make static
make test-fast
make release-sanity
make release-review       # clean committed review branch
make release-candidate    # exact merged main only
```

None of these commands creates a commit, tag, push or release.
