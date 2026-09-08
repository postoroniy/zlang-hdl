# Releasing ZLang HDL

This document describes the public release gate. The first release target is a
GitHub alpha; publishing to PyPI or a container registry is a separate decision.

## Preconditions

- Complete the [licensing/public release checklist](docs/licensing/PUBLIC_RELEASE_CHECKLIST.md)
  against the exact candidate, including generated output and reference-IP provenance.
- Release only from the reviewed public snapshot branch with a clean worktree.
- The package version, CLI versions, changelog entry, and tag must agree.
- Bind the [2026-09 Community Baseline](docs/licensing/COMMUNITY_BASELINE.md)
  to the exact reviewed public commit, signed tag, projection manifest and
  source/archive hashes in the release evidence. Until then it is a release
  designation, not an already-published snapshot. Retain every baseline
  capability and all accepted compiler fixes; no Enterprise extraction belongs
  in this release. Include the approved [edition matrix](docs/editions.md).
- Confirm that the GitHub source snapshot contains `LICENSE`, `NOTICE`, REUSE
  metadata, `TRADEMARKS.md`, `docs/project-scope.md`, public-tree policy, and
  every nested third-party notice. The runtime
  wheel and sdist must contain the root `LICENSE` and `NOTICE`; examples, tests,
  internal release policy, and their nested notices are intentionally not part
  of those runtime archives.
- Confirm that no private development history, host-specific path, credential,
  scratch artifact, or internal coordination document is present.
- Review the public repository description, topics, and detected license. Do not
  publish a homepage or documentation domain until that domain is controlled by
  the project maintainer.
- Review third-party license inventory, generated SBOM, and secret scan results.
- Confirm that release notes and documentation contain no link to a tag or
  release that has not yet been published.
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

Pin `pip==26.2.1` in the release build environment. Create package-test venvs
with `--without-pip` and install through that patched parent pip using
`python -m pip --python <venv>/bin/python install pip==26.2.1 ...`.
Do not silently use setup-python's bundled installer in the shipped inventory.

Audit the exact `dist/release-requirements.txt` with
`tools/release_inventory.py` before SBOM/checksum generation, attestation and
artifact upload. This includes installer tooling as well as runtime packages.
Require the complete wheel and sdist installation inventories to match before
publishing the shared inventory and SBOM.
Only the exact local ZLang distribution is excluded from remote vulnerability
lookup, because it has not been published to PyPI. Every other pinned package
must have a complete, version-matching, unskipped audit result and no findings.
Project-only `pip-audit .` is an additional check, not a substitute for this gate.

The release also carries the independently versioned lexical editor VSIX and
its JSON audit report. Build them from the same verified tag with pinned Node
22.23.2 and locked `npm ci --ignore-scripts`. Before tokenization and packaging,
rerun a fresh `npm audit --package-lock-only --include=dev --include=optional
--include=peer --audit-level=info --json` over the complete exact-tag lockfile,
including all build tooling. Require zero findings at every severity and a
dependency total matching every lockfile package except the root project; an
unavailable, malformed or incomplete audit fails the release. Retain
`build/editor-npm-audit.json` and the exact Python inventory audit in the
30-day Actions `release-<tag>-dependency-audits` artifact; archive them with the
maintainer's durable release evidence. They are not added to the runtime/VSIX
download payload. The install-time audit warning
and pull-request dependency review do not replace this full release-time gate.
Require passing real TextMate/Oniguruma tests, then audit the actual VSIX against
the tagged source, including the exact
static inventory and authoritative LICENSE/NOTICE bytes, before copying either
file into the release payload. Both files belong in `SHA256SUMS` and artifact
attestation coverage. Rerun the VSIX audit from the verified exact tag and verify
the GitHub attestation against that source commit, tag and release workflow: the
standalone audit JSON establishes payload/source byte agreement, not commit
provenance. The Python SBOM describes the Python inventory; npm
tooling is build-only, and no runtime dependency or LSP is shipped in the VSIX.
The editor version remains independent of the compiler alpha version. Attaching
the VSIX to this GitHub release does not publish it to Marketplace or Open VSX.

Validate the immutable public checkout before installing build dependencies.
Installed npm tooling and generated package environments are not public source;
validate a fresh clean export when rechecking the publication manifest.

Install the release tools in the active Python 3.12 environment, then build from
a clean checkout with a fixed `SOURCE_DATE_EPOCH`:

```bash
.venv/bin/python -m pip install --upgrade pip==26.2.1
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
4. Publish a GitHub release containing wheel, source distribution, the lexical
   editor VSIX and its audit JSON, `SHA256SUMS`, SBOM, provenance attestation,
   and release notes. VSIX ZIP bytes are not claimed reproducible: verify the
   actual hosted artifact's hash, exact-tag audit rerun and GitHub source-bound
   attestation before publication.
5. Verify the published artifacts in a fresh environment.

Do not publish a tag or artifact from the private development branch, a dirty
worktree, or an unreviewed generated snapshot.

The Apache-2.0 project boundary is documented in
[docs/project-scope.md](docs/project-scope.md), and use of the project name is
described in [TRADEMARKS.md](TRADEMARKS.md). Neither document substitutes for
the license review required by this release gate.
