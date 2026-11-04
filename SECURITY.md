# Security policy

## Supported versions

`0.1.0a24` is the final planned public alpha release; it remains a candidate
until publication. No ongoing public maintenance, response time or future fix
is committed. Maintainers may review security reports and consider a correction
at their discretion. Existing releases and licence grants remain unchanged.

| Version | Report and maintenance policy |
| --- | --- |
| Public a24 candidate/final baseline | Reports may be reviewed; no ongoing support or fix commitment |
| Older snapshots and alphas | Not maintained; include the affected version when reporting |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
vulnerability reporting for the repository:

<https://github.com/postoroniy/zlang-hdl/security/advisories/new>

Include the affected version or commit, a minimal reproducer, expected impact,
and any known mitigation. Maintainers may coordinate disclosure where practical and credit reporters who
request attribution. This channel remains available without a guaranteed review
or response deadline.

## Security boundary

ZLang HDL source files, project manifests, verification bundles, generated HDL, and
third-party dependencies should be treated as untrusted input unless their
origin is known. Optional flows invoke external tools such as Verilator,
Icarus Verilog, Yosys, SymbiYosys, vendor tools, and SMT/model-checking solvers;
neither ZLang nor those tool invocations are a
security sandbox. Run untrusted designs and verification bundles in an isolated
environment with appropriate resource limits.

Toolchain correctness bugs, malformed output, path traversal, unsafe project or
lock resolution, command execution, secret disclosure, and dependency
vulnerabilities are all appropriate subjects for a private report.
