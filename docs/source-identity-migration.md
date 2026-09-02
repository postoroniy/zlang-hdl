# ZLang HDL source identity

The public product name is **ZLang HDL**. Its canonical filesystem and tooling
identity is:

| Surface | Identity |
|---|---|
| Distribution and repository | `zlang-hdl` |
| Compiler command | `zlang` |
| Source suffix | `.zhl` |
| VS Code language id | `zlang-hdl` |
| MIME type | `text/x-zlang-hdl` |

The former `.zl` suffix is not a compatibility alias. Physical compiler inputs
using it fail with diagnostic `ZL-SOURCE-EXTENSION` and must be renamed. This
strict boundary avoids collision with the unrelated `zlangdevs/zlang` project,
which already uses `.zl`.

Logical imports do not contain a source suffix. For example,
`import std.math.fixed` resolves to `stdlib/math/fixed.zhl`, while project and
locked-package imports use their logical module identities.

The dependency-lock schema is version 2 after this migration. Older locks are
rejected and must be regenerated with `zlang-lock update`; this prevents a lock
that names the former physical suffix from being accepted under a new source
identity. Source, build, and proof caches miss safely because their dependency
and source-unit identities include the migrated paths.

The short prose name **ZLang** remains valid after the product has been
introduced. The Python package `zlang`, `zlang.toml`, `zlang.lock`, the
compiler-owned `.zlang/` state directory, environment variables prefixed
`ZLANG_`, generated HDL identifiers, and the TextMate scope `source.zlang` are
intentional internal identities and were not renamed.
