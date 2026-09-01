# Security policy

## Supported versions

ZLang is currently an experimental alpha. Security fixes are applied to the
latest published alpha release and the current public `main` branch. Older
alphas are not maintained unless a release note explicitly says otherwise.

| Version | Supported |
| --- | --- |
| Latest `0.1.x` alpha | Yes |
| Older snapshots and alphas | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
vulnerability reporting for the repository:

<https://github.com/postoroniy/zlang-hdl/security/advisories/new>

Include the affected version or commit, a minimal reproducer, expected impact,
and any known mitigation. Maintainers will acknowledge the report when it has
been reviewed, coordinate disclosure where practical, and credit reporters who
request attribution.

## Security boundary

ZLang source files, project manifests, verification bundles, generated HDL, and
third-party dependencies should be treated as untrusted input unless their
origin is known. The compiler invokes external tools such as Clash, Verilator,
Yosys, SymbiYosys, and solvers; neither ZLang nor those tool invocations are a
security sandbox. Run untrusted designs and verification bundles in an isolated
environment with appropriate resource limits.

Toolchain correctness bugs, malformed output, path traversal, unsafe project or
lock resolution, command execution, secret disclosure, and dependency
vulnerabilities are all appropriate subjects for a private report.
