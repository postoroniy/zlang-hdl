# Contributing to ZLang HDL

`0.1.0a24` is the final planned public alpha release; it remains a candidate
until publication. Public contributions are limited to focused bug/security
reports, documentation corrections and independently testable maintenance fixes.
Acceptance and future fixes are at the maintainer's discretion. New features,
experimental compiler capabilities and further public feature releases are not
planned. Existing licences and contribution provenance requirements remain unchanged.

## Before opening a change

- Search existing issues and the [language support matrix](docs/language-reference.md#reference-syntax-support-matrix).
- Keep one pull request focused on one independently testable change.
- Do not start a public language, IR, backend or formal-semantics feature change;
  it is outside this maintenance scope.
- Do not weaken fail-closed diagnostics, formal expectations, or existing tests
  to make a new feature pass.
- State the origin and license of any third-party code, fixtures, algorithms, or
  generated data included in the change.

## Development setup

ZLang's supported development environment is Linux x86-64 with CPython
`>=3.12,<3.13`. The recommended `uv` workflow provisions that runtime without
requiring a version-suffixed Python command from the host:

```bash
uv python install 3.12
make venv PYTHON_PROFILE=dev
source .venv/bin/activate
.venv/bin/python -m pytest -q
```

Each checkout and Git worktree owns its own `.venv`; never borrow an
interpreter from a sibling checkout.  The supported bootstrap command is
`make venv PYTHON_PROFILE=dev`, followed by `source .venv/bin/activate` for interactive work.
The default `make venv` retains the full release setup; runtime-only use is
`make install`. Profiles add dependencies and never remove
existing packages. Before release/audit gates, install the complete profile with
`make venv PYTHON_PROFILE=release INSTALL_EDITOR=0`.
Make targets always select the local environment themselves and fail closed if
an unrelated `VIRTUAL_ENV` is active.

See the [installation chapter](docs/language-reference.md#reference-installing-toolchain) for a conventional
`venv`/pip alternative, WSL2 setup and optional external EDA tools.

Focused tests should be run serially while debugging. The repository Makefile
provides the canonical local gates:

```bash
make static
make test-fast
make test
```

`WORKERS` is overridable, for example `make test WORKERS=4`; `PYTHON` is
intentionally pinned to this checkout's `.venv`. `make release-candidate` runs the license
and dependency audits, pinned-tool check, two-pass zero-skip regression, editor
tests and reproducible package build, but never creates a tag or publishes
anything.

Tests requiring Verilator, Yosys, SymbiYosys, Z3, or Icarus Verilog must report tool
absence explicitly. Release and dedicated toolchain jobs require the pinned
tools and do not accept an unexpected skip.

Before calling a source change complete:

1. Compile the exact top with `--check`.
2. Run focused parser, semantic, canonical, and simulator tests.
3. Exercise production Direct-SV with strict Verilator.
4. Run applicable formal tests without inventing new claims.
5. Run the regression gate and `git diff --check` required by the change.

## Manual PDFs

The Language Reference, Standard Library Guide and User Guide share the chapter
sources and book mapping in `docs/manuals.toml`. Release maintainers build the
three PDFs with the host-owned renderer and reviewed cover asset. Documentation
contributions update those Markdown sources and exercise their examples. The
release process builds the complete set twice, validates each book's metadata
and requires an all-page visual review. The builder is separate from ordinary
installation and use.

## Pull requests

A pull request should include:

- the reason for the change and its user-visible behavior;
- focused positive and negative tests;
- backend, simulator, canonical-IR, and formal coverage where applicable;
- documentation for changed public behavior;
- a note describing compatibility and any deliberate deferral.

All commits must satisfy the
[Developer Certificate of Origin 1.1](https://developercertificate.org/).
Add a sign-off using your real name and an email address you control:

```text
Signed-off-by: Your Name <you@example.com>
```

Git can add the line automatically:

```bash
git commit --signoff
```

By contributing original work, you certify its provenance through the DCO and
agree that it is submitted under the repository's
[Apache License 2.0](LICENSE), unless an existing subtree or file clearly
documents another compatible license. In particular, contributions to
`examples/projects/80211a_transmitter/` are submitted under that subtree's MIT
license. Third-party material must retain its applicable copyright, license,
and notice information; a DCO sign-off does not replace those obligations.

The project does not require a Contributor License Agreement (CLA).

## Conduct and security

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
Security issues must be reported privately according to [SECURITY.md](SECURITY.md),
not through a public issue.
