<a id="reference-structured-diagnostics"></a>
# Structured diagnostics


Every compiler error retains its human-readable message and may also
carry a stable diagnostic code, one primary `SourceOrigin`, notes, and suggested
fixes. `SourceOrigin` always has a half-open source span and construct; its
logical source unit and lowercase SHA-256 digest are optional in the base record.
A *complete* origin has all four fields. Compiler-shipped
declarations use logical units such as `std.math.complex`; ordinary CLI inputs
use the path supplied to `zlang`.

Text remains the default and is compatible with existing scripts:

```sh
zlang design.zhl --check
```

The `fixes` values in the structured diagnostic are human-readable suggestions,
not source edits. Machine-applicable edits have a separate compiler-owned
metadata and tooling projection described in
[compiler-owned diagnostic edits](tooling-integration-api.md#reference-tooling-integration-api-diagnostic-edits);
consumers must never derive edits from these strings.

Machine consumers select one deterministic JSON object:

```sh
zlang design.zhl --check --diagnostic-format json
```

The JSON object has `schema`, `severity`, `code`, `message`, `primary`,
`notes`, and `fixes`. Older exception-string users continue to receive the same
`str(error)`. Categories initially carrying specific codes include parsing,
imports, width assignment, timing alignment, domain crossing, protocol
ownership/type, top selection, I/O, and backend binding failures. Unmigrated
errors use a stable generic category rather than inferring semantic meaning
from diagnostic text.

Backend output can additionally publish a hash-bound
[generated source map](generated-source-maps.md#reference-generated-source-maps). External-tool attribution is
accepted only when the generated text hash matches the sidecar and exactly one
entry covers the reported line. Otherwise the original Verilator/Yosys/vendor
diagnostic is left unchanged.
