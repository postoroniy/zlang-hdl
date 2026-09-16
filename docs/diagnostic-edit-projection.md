# Compiler-owned diagnostic edit projection

The diagnostic-edit audit added a narrow, machine-applicable edit path below
the LSP protocol boundary. It added no LSP method or capability. The LSP layer
consumes the accepted projection mechanically as `textDocument/codeAction`;
it does not add or infer fix semantics.

## Existing model and audit

Before this slice, `Diagnostic.fixes` and `ToolingDiagnostic.fixes` were tuples
of human-readable strings. They remain suggestions: they do not identify a
replacement range or replacement text and must never be interpreted as edits.
The public `zlang-diagnostic-v1` JSON representation is unchanged.

The following table covers every current fix-producing call site. A grouped
row may have more than one producer with the same diagnostic code and safety
reason.

| Diagnostic | Classification | Reason |
|---|---|---|
| `ZL-IMPORT-DUPLICATE` for an identical path and alias | machine-applicable | The parser owns the exact second import-declaration span; deletion preserves the remaining identical import. |
| `ZL-IMPORT-DUPLICATE` for different aliases | semantically unsafe | Choosing which alias/import to retain can change visible names. |
| `ZL-SOURCE-EXTENSION` | suggestion only | Renaming a filesystem object is not a source-text edit. |
| `ZL-IMPL-001` unknown profile | suggestion only | The remedy belongs to manifest/profile selection and requires user choice. |
| `ZL-IMPL-001` conflicting policy | semantically unsafe | Either policy may be removed or changed. |
| `ZL-INTERFACE-CONFORMANCE` | insufficient range information / semantically unsafe | The public signature can require several user-selected changes. |
| `ZL-IMPORT-ALIAS-DUPLICATE` | semantically unsafe | A unique replacement alias is not compiler-determined. |
| `ZL-IMPORT-ALIAS-UNKNOWN` | semantically unsafe | The desired import and alias are user intent. |
| `ZL-IMPORT-RESOLVE` | suggestion only | The remedy may update the project index or lock rather than source text. |
| `ZL-SEMANTIC-PARAMETER-CONSTRAINT` | semantically unsafe | Concrete specialization values are not uniquely implied. |
| `ZL-DOMAIN-CROSSING` | semantically unsafe | Crossing kind, endpoints and placement are design choices. |
| `ZL-SEMANTIC-CONDITIONAL-ACTION-CONFLICT` | semantically unsafe | Rewriting rule structure or priority may change behavior. |
| `ZL-WIDTH-ASSIGNMENT` | semantically unsafe | Conversion width/policy and truncation intent are not uniquely implied. |
| `ZL-SEMANTIC-OUTPUT-READ` | insufficient range information / semantically unsafe | The suggested local binding is a multi-site refactor without a unique name or placement. |
| `ZL-WIDTH-CONCAT` | semantically unsafe | The operand and resize/bitcast policy require user intent. |
| `ZL-SEMANTIC-RUNTIME-LOGIC` | insufficient range information / semantically unsafe | The AST retains the full binary-expression span, not an exact operator span, and the bitwise replacement precondition is not established first. |
| `ZL-TIMING-MISMATCH` | semantically unsafe | Stage placement and alignment are architectural choices. |
| `ZL-TIMING-CONTRACT` | semantically unsafe | Either implementation or declared contract may be changed. |
| `ZL-BACKEND-BINDING` | suggestion only | Resource selection is implementation policy, not a local source replacement. |
| `ZL-BACKEND-SYSTEMVERILOG-UNCLAIMED-STATE` | insufficient range information / semantically unsafe | Moving state into a child module is a design refactor. |

Diagnostics with no `fixes` metadata are classified as **no fix**. Notes,
messages and diagnostic codes are never parsed to manufacture an edit.

## Compiler fix model

`DiagnosticEdit` contains one exact `SourceOrigin` and replacement text.
Machine-applicable origins must carry both a source unit and source digest. An
empty replacement is a deletion; equal start/end coordinates can represent an
insertion when a future compiler diagnostic explicitly owns that insertion
point.

`DiagnosticFix` contains a title and a non-empty tuple of edits. The tuple is
one atomic fix, while multiple `DiagnosticFix` records remain distinct
alternatives. These records are carried by `DiagnosticError.machine_fixes`.
The field name is the applicability contract: legacy `fixes` strings remain
suggestions, while only `machine_fixes` may reach the edit projection.

The first producer is deliberately narrow. `ZL-IMPORT-DUPLICATE` supplies one
deletion only when a later import has the same logical path and the same alias
as an earlier import. The edit targets the parser-owned span of that later
declaration. Duplicate paths with different aliases retain the existing
diagnostic but expose no automatic edit.

## Tooling projection

`TOOLING_DIAGNOSTIC_EDIT_SCHEMA` is `1`. Each `ToolingDiagnostic` may now
contain immutable `ToolingDiagnosticFix` records, whose edits are immutable
`ToolingDiagnosticEdit` values containing an absolute current-source path,
authoritative `ToolingOrigin` and replacement text.

Projection is fail closed. The complete fix is omitted unless every edit:

- matches the SHA-256 digest of the exact snapshot text;
- has a valid half-open compiler span inside that snapshot;
- targets the current source passed to `check_snapshot`.

The projection intentionally rejects cross-file edits. A partially valid
multi-edit fix is never projected. Tooling keeps compiler-native one-based
coordinates; LSP UTF-16 conversion belongs in the LSP layer.

## LSP mapping

For each request, the Community LSP rechecks the exact current in-memory source
with `check_snapshot()`. Only current diagnostics whose ranges intersect the
requested range and match any client-supplied diagnostic identity are
considered. Every `ToolingDiagnosticFix` becomes one edit-only `quickfix`
`CodeAction`; all edits in that fix remain together in one `WorkspaceEdit`, and
separate alternatives remain separate actions. Replacement strings and exact
spans are preserved. Stale diagnostics, cross-file edits, prose suggestions,
and diagnostics without `machine_fixes` produce no action.

## Validation boundary

The duplicate-import test applies the exact projected deletion in memory and
runs the normal semantic-only snapshot check again. The duplicate-import
diagnostic disappears and the source passes. Separate negative tests prove
that a width-conversion suggestion and a duplicate import with different
aliases do not acquire machine edits.

This path performs parsing, import resolution and semantic checking only. It
does not run optimization, implementation selection, RTL generation,
synthesis, formal tools or external processes.
