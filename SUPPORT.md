# Support

ZLang HDL is an experimental open-source alpha. Community support is best-effort;
there is no response-time or compatibility SLA.

## Where to ask

- Usage questions and design discussion: use GitHub Discussions at
  <https://github.com/postoroniy/zlang-hdl/discussions>.
- Reproducible compiler defects: use the structured issue forms at
  <https://github.com/postoroniy/zlang-hdl/issues/new/choose>.
- Security vulnerabilities: follow [SECURITY.md](SECURITY.md) and report them
  privately.
- Conduct concerns: follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

Before reporting a compiler problem, include:

- the ZLang version or exact commit;
- operating system and Python version;
- a minimal `.zhl` reproducer;
- the complete command and structured diagnostic;
- external tool versions when Clash, Verilator, Yosys, SBY, or a solver is
  involved.

The [current language status](docs/current-language-status.md),
[syntax matrix](docs/syntax-support-matrix.md), and
[backend/tooling guide](docs/backends-tooling.md) describe supported and
fail-closed behavior. A documented unsupported feature is not necessarily a
bug, but a concrete real-design reproducer is useful evidence for prioritizing
future work.
