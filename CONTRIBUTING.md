# Contributing to ZLang

Thank you for helping improve ZLang. The project welcomes focused bug reports,
documentation corrections, tests, and bounded compiler changes.

## Before opening a change

- Search existing issues and the [current capability matrix](docs/current-language-status.md).
- Keep one pull request focused on one independently testable change.
- Discuss broad language, IR, backend, or formal-semantics changes in an issue
  before implementation.
- Do not weaken fail-closed diagnostics, formal expectations, or existing tests
  to make a new feature pass.
- State the origin and license of any third-party code, fixtures, algorithms, or
  generated data included in the change.

## Development setup

ZLang's initial supported development environment is Linux x86-64 with Python
3.12.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Focused tests should be run serially while debugging. The complete suite may be
run in parallel:

```bash
.venv/bin/python -m pytest -n 8 --dist=loadscope -q
.venv/bin/python -m compileall -q zlang tests
git diff --check
```

Tests requiring Clash, Verilator, Yosys, SymbiYosys, or Z3 must report tool
absence explicitly. Release and dedicated toolchain jobs require the pinned
tools and do not accept an unexpected skip.

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

By contributing, you agree that your contribution is submitted under the
repository's [Apache License 2.0](LICENSE), unless a file clearly documents a
compatible third-party license.

## Conduct and security

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
Security issues must be reported privately according to [SECURITY.md](SECURITY.md),
not through a public issue.
