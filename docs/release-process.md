# Community release process

ZLang Community releases use one reviewed source tree and two modes of the
same GitHub `Release` workflow. The candidate mode validates and retains
artifacts but cannot publish. Only a signed annotated tag at the exact protected
`main` commit enables the publish job.

Branch ownership, durable regression requirements, release inclusion, and
post-publication branch retention are mandatory in
[`branch-release-regression-policy.md`](branch-release-regression-policy.md).
In particular, a fix is not release evidence until it is represented in the
candidate's `release/regressions.json` with permanent tests.

## Local candidate

Create a release branch from the latest public `main`. Update the compiler,
native dependency, native wheel, changelog, reference/version metadata and
release status to the same prospective version. Then commit the complete
candidate and run:

```bash
make release-candidate \
  TAG=v0.1.0a19 \
  PREVIOUS_TAG=v0.1.0a18 \
  BUILD_ROOT=build/a19-local \
  EDITOR_VSIX=build/a19-editor/zlang-hdl-0.1.0.vsix
```

This target does not commit, tag, push or publish. It requires a clean checkout
and runs the release identity preflight, public-tree/status checks, static and
native audits, two no-skip suites, external-tool inventory, an installed-VSIX
host smoke, and reproducible package construction.

`tools/release_preflight.py` composes existing authoritative validators. Its
deterministic JSON binds the package version, prospective tag, previous tag,
Git commit/tree, changelog notes, reviewed PDF, editor lock/package, release
status, regression ledger and audited native wheel. It never creates a tag or
release.

## Hosted pre-tag gate

After the release PR is reviewed and its required checks pass, merge it. Run the
`Release` workflow manually on the exact `main` commit with:

- `tag`: the prospective tag, for example `v0.1.0a19`;
- `previous_tag`: the exact prior release, for example `v0.1.0a18`.

Manual dispatch runs validation and EDA jobs, uploads review artifacts and does
not attest or publish. It fails if the prospective tag already exists, is not
the immediate alpha successor, or the selected commit is not descended from
the previous tag.

## Signed publication

Only after the hosted candidate gate passes, create a signed annotated tag at
the exact reviewed `main` commit and push that tag. The tag-triggered workflow
repeats all release checks, verifies GitHub's signature and exact-main identity,
attests the accepted checksums, and publishes the GitHub prerelease.

Never rewrite an existing release tag. If a tag-triggered gate fails after the
tag exists, fix the cause through a reviewed commit and prepare the next alpha.
