# Releasing ZLang HDL

This document describes the public release gate. The first release target is a
GitHub alpha; publishing to PyPI or a container registry is a separate decision.

## Preconditions

- Release only from the reviewed public snapshot branch with a clean worktree.
- The package version, CLI versions, changelog entry, and tag must agree.
- Confirm that the GitHub source snapshot contains `LICENSE`, `NOTICE`, REUSE
  metadata, public-tree policy, and every nested third-party notice. The runtime
  wheel and sdist must contain the root `LICENSE` and `NOTICE`; examples, tests,
  internal release policy, and their nested notices are intentionally not part
  of those runtime archives.
- Confirm that no private development history, host-specific path, credential,
  scratch artifact, or internal coordination document is present.
- Review third-party license inventory, generated SBOM, and secret scan results.
- Enable GitHub Discussions, private vulnerability reporting, secret scanning,
  push protection, Dependency Graph, and Dependabot alerts before accepting
  public contributions. Keep default Actions token permissions read-only.
- Install the DCO App for this repository, open a real signed-off pull request,
  and require its successful DCO status before that pull request is merged. A
  dummy merged pull request is neither necessary nor sufficient release
  evidence.
- Protect `main` and `v*` tags with linear history, no force pushes, CODEOWNER
  review, and the required CI/security/DCO checks.
- Use a dedicated or disposable self-hosted EDA runner. It must not contain the
  private development checkout, a signing key, or unrelated credentials, and it
  must not run pull-request code.

## Acceptance gate

Run the repository's fast and pinned-tool CI on the exact release commit. The
release candidate must include:

- two complete parallel pytest runs with no unexpected skips;
- `compileall`, Ruff correctness checks, documentation links, and diff checks;
- clean source and wheel installations with all installed CLI entry points;
- strict direct-SystemVerilog/Verilator coverage;
- real Clash generation and the pinned Yosys/SBY/Z3 verification and mutation
  smoke tests;
- deterministic generated artifacts, verification-bundle replay, and package
  contents.

Install the release tools in the active Python 3.12 environment, then build from
a clean checkout with a fixed `SOURCE_DATE_EPOCH`:

```bash
.venv/bin/python -m pip install build==1.3.0 setuptools==84.0.0 \
  twine==6.2.0 wheel==0.46.3
SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)" \
  .venv/bin/python -m build --no-isolation
.venv/bin/python -m twine check dist/*
```

Install the wheel and source distribution into separate empty virtual
environments and exercise `zlang`, `zlang-lock`, `zlang-verify`, and
`zlang-compare-backends` outside the checkout.

## Publication

1. Replace `Unreleased` on the release's changelog entry with the release date.
2. Create a signed, annotated tag named `v<version>` on the verified commit.
3. Rebuild from that tag and require the wheel to be byte-identical to the
   candidate wheel. The current setuptools sdist contains generated timestamps,
   so it is rebuilt and content-validated but is not claimed byte-reproducible.
4. Publish a GitHub release containing wheel, source distribution,
   `SHA256SUMS`, SBOM, provenance attestation, and release notes.
5. Verify the published artifacts in a fresh environment.

Do not publish a tag or artifact from the private development branch, a dirty
worktree, or an unreviewed generated snapshot.
