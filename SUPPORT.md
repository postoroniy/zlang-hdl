# Support

`0.1.0a24` is the final planned public alpha release and remains a candidate
until publication. The repository is retained for use, reproducibility and
historical reference. No further public feature release or ongoing support is
committed; maintainers may review bug and security reports at their discretion.
Existing licence grants remain unchanged.

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
- external tool versions when Verilator, Icarus, Yosys, SBY, a vendor tool, or
  a solver is involved.

The [language support matrix](docs/language-reference.md#reference-syntax-support-matrix)
and [compiler reference](docs/language-reference.md) describe supported and
fail-closed behavior. A documented unsupported feature is not necessarily a
bug. New features and experimental compiler work are outside the public
maintenance scope; a report does not create a commitment to implement them.
