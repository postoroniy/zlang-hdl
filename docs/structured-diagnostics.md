# Structured diagnostics and source provenance

Every compiler error retains its historical human-readable message and may also
carry a stable diagnostic code, one primary `SourceOrigin`, notes, and suggested
fixes. A complete origin contains the logical source unit, the source SHA-256,
the half-open source span, and the construct being diagnosed. Compiler-shipped
declarations use logical units such as `std.math.complex`; ordinary CLI inputs
use the path supplied to `zlangc`.

Text remains the default and is compatible with existing scripts:

```sh
zlangc design.zl --check
```

Machine consumers select one deterministic JSON object:

```sh
zlangc design.zl --check --diagnostic-format json
```

The version-1 object has `schema`, `severity`, `code`, `message`, `primary`,
`notes`, and `fixes`. Older exception-string users continue to receive the same
`str(error)`. Categories initially carrying specific codes include parsing,
imports, width assignment, timing alignment, domain crossing, protocol
ownership/type, top selection, I/O, and backend binding failures. Unmigrated
errors use a stable generic category rather than inventing meaning from text.

Backend output can additionally publish a hash-bound
[generated source map](generated-source-maps.md). External-tool attribution is
accepted only when the generated text hash matches the sidecar and exactly one
entry covers the reported line. Otherwise the original Clash/Verilator
diagnostic is left unchanged.
