<a id="reference-source-identity-migration"></a>
# Source and product identity


The public product name is **ZLang HDL**. Its distribution and repository are
`zlang-hdl`; the compiler command is `zlang`, the source suffix is `.zhl`, the
VS Code language ID is `zlang-hdl`, and the MIME type is `text/x-zlang-hdl`.

The `.zl` suffix is not a compatibility alias. Physical compiler inputs using
it fail with diagnostic `ZL-SOURCE-EXTENSION`; use `.zhl`.

Logical imports do not contain a source suffix. For example,
`import std.math.fixed` resolves to `stdlib/math/fixed.zhl`, while project and
locked-package imports use their logical module identities.

Older dependency locks are rejected by the current versioned lock schema;
regenerate them with `zlang lock update`. Source, build, and proof caches are
bound to dependency and source-unit identities, and stale inputs miss safely.

The short prose name **ZLang** remains valid after the product has been
introduced. The Python package `zlang`, `zlang.toml`, `zlang.lock`, the
compiler-owned `.zlang/` state directory, environment variables prefixed
`ZLANG_`, generated HDL identifiers, and the TextMate scope `source.zlang` are
intentional product identities.
