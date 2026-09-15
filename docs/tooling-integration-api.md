# Tooling integration API

ZLang HDL exposes a small, read-only Python integration surface in
`zlang.tooling`. It is intended for editors, build coordinators and other
compiler-aware tools that must not depend on parser, workspace or semantic IR
implementation modules.

Consumers must check `TOOLING_API_SCHEMA`; the package version alone is not a
compatibility guarantee. The version-1 API publishes immutable records for:

- compiler, source-suffix and capability identity;
- source declarations and direct-import resolution;
- the immutable document-symbol projection for the current source text;
- the immutable semantic-hover projection for one source position;
- the immutable compiler-resolved definition projection for one source position;
- the immutable compiler-resolved reference projection for one source position;
- the immutable semantic rename-edit projection for one source position;
- the immutable compiler-visible semantic completion projection for one source
  position;
- the immutable compiler-resolved signature-help projection for one source
  position;
- the immutable compiler-classified semantic-token projection for one source;
- compiler-owned machine-applicable diagnostic fixes for semantic checks;
- read-only project/workspace indexing and dependency closure;
- one exact semantic-only source-snapshot check.

These calls do not emit RTL, run formal tools, synthesize a design or mutate a
project lock. A failed semantic check is returned as a structured record. An
environment or malformed-project failure that prevents a trustworthy record
raises `ToolingError`.

## Demand-driven observational analysis

The semantic checker separates language correctness from optional editor
observations.  `check_file_snapshot()` accepts the compiler-owned
`zlang.analysis_needs.AnalysisNeeds` bitmask; the default is `NONE`, so a
normal compiler check does not construct editor records.  The tooling
projections request only what they consume:

| query | demanded records |
| --- | --- |
| `definition_at`, `references_at`, `rename_at`, `semantic_tokens` | `DEFINITIONS` |
| `completion_at` | `DEFINITIONS + COMPLETION` |
| `signature_help_at` | `SIGNATURE_HELP` |
| `hover_at`, `check_snapshot` | none |

`DEFINITIONS` covers the shared compiler identity/occurrence records used by
definition, references, rename and semantic-token projections.  Completion
scope/candidate/detail collection is therefore not entered by a
definition-only request.  The needs mask controls observational sinks only;
typed semantic checking, canonical IR and all backend products remain
unchanged. There is no process-global semantic cache or incremental analysis
layer; the normalized persistent symbol cache is described below.

The audit found no separate eager record for hover: hover projects the already
required AST/typed IR product.  Likewise, references, rename and semantic
tokens do not need independent collectors; they project the definition
identity/occurrence records.  The previously eager records were therefore
`definition_resolutions`/`definition_declarations`, `completion_scopes` (and
its candidate/detail builders), and `signature_help_calls`.  All are now
allocated only when their corresponding need is present.

The API is intentionally generic. It does not expose private product features,
session state, autonomous behavior or a plugin-specific command surface.

## Diagnostic edits

`TOOLING_DIAGNOSTIC_EDIT_SCHEMA` is currently `1`. A failed
`check_snapshot()` record retains the existing prose-only
`ToolingDiagnostic.fixes` and may additionally contain immutable
`ToolingDiagnosticFix` records. Each machine fix preserves its compiler-owned
title and one atomic tuple of `ToolingDiagnosticEdit` records containing the
absolute current-source path, exact compiler origin and replacement text.

Only explicit `DiagnosticError.machine_fixes` metadata is projected. Tooling
does not parse diagnostic messages, codes, notes or legacy fix prose. The
projection requires every edit to match the exact snapshot digest and a valid
half-open span; otherwise the entire fix is omitted. The first and currently
only producer deletes the later of two imports with identical logical path and
alias. Cross-file machine edits are not projected in this schema.

The compiler/tooling records remain protocol neutral. They are not LSP
`TextEdit`, `WorkspaceEdit`, `CodeAction` or `Command` values. Milestone 11
leaves this schema unchanged and mechanically maps current-source machine fixes
to edit-only LSP quick fixes after rechecking the current editor snapshot. The
full audit and safety rationale are recorded in
[Compiler-owned diagnostic edit projection](diagnostic-edit-projection.md).

## Generated RTL navigation audit

Milestone 12 adds no tooling navigation projection. The existing
`GeneratedSourceMap` v1 is authoritative for hash-verified generated-line to
source-origin attribution, but its bounded builder currently maps only one
unique top-level output assignment. It has no generated artifact path or
artifact-discovery contract, and no query currently validates a map against the
current editor source digest. Recursive `BackendArtifact` bindings describe
semantic/physical signals and hierarchy, not generated line locations.

Consequently `zlang.tooling` does not expose raw `BackendArtifact` or source-map
objects and does not generate RTL on a tooling request. The exact capability
and prerequisite are recorded in [Generated source maps](generated-source-maps.md).

M13 adds the protocol-neutral
`zlang.generated_navigation_bundle.load_generated_navigation_bundle()`
boundary. It validates one explicit relocatable publication directory, exact
generated/backend-manifest/source-map hashes, lineage, and complete producing
source snapshot identities. The returned immutable record can classify current
source text or digest as `match`, `stale`, or `unknown_source`. This is artifact
infrastructure only: `zlang.tooling` exposes no generated-location query yet,
and loading never invokes compilation.

## Document symbols

`TOOLING_DOCUMENT_SYMBOL_SCHEMA` is currently `1`. The
`document_symbols(source_text)` function returns a tuple of immutable
`ToolingSymbol` records. Each record contains a declaration name, a stable
compiler-owned category, an authoritative `ToolingOrigin` where one is
available, an authoritative selection range (currently the same origin when
the parser does not retain a narrower name span), and recursively projected
children where the parser exposes containment.

This is deliberately a narrow document-structure projection. It does not
export AST nodes, typed IR, references, definitions, widths, protocols, or
implementation plans. It uses the existing parser through the tooling module;
the LSP must not parse or scan source text independently. If parsing fails for
an incomplete editor buffer, `document_symbols` returns an empty tuple rather
than guessing declarations. The existing `TOOLING_API_SCHEMA` remains `1`
because this is an additive, separately versioned projection. Parser records
that carry no stable declaration identity are omitted rather than assigned a
synthetic symbol name.

## Semantic hover

`TOOLING_HOVER_SCHEMA` is currently `1`. The
`hover_at(source, source_text, line, character)` function accepts a stable
source path and a zero-based editor position, then returns either an immutable
`ToolingHover` record or `None`. The record contains only narrow
compiler-owned facts: selected name and kind, canonical type text, width,
signedness, fixed-point type text, port direction, an available callable
signature, and its authoritative `ToolingOrigin`.

The function invokes the semantic-only compiler snapshot path. It does not
invoke optimization, implementation selection, RTL generation, synthesis, or
formal execution, and it never returns AST or typed-IR objects. Positions with
no semantic entity and malformed/incomplete source return `None`; workspace,
resolver, and other environment failures raise `ToolingError` so callers cannot
mistake an unavailable project for a valid empty hover.

## Definitions

`TOOLING_DEFINITION_SCHEMA` is currently `1`. The
`definition_at(source, source_text, line, character)` function asks the
semantic compiler snapshot for its resolved source-definition records and
returns either an immutable `ToolingDefinition` or `None`. A definition
contains only the resolved name/kind, the locked physical target path, and the
authoritative target `ToolingOrigin`.

The projection currently covers compiler-resolved value/port references and
ordinary or generic function calls, named types, enum members, registers,
immutable locals, and module-instance targets when their declaration origin is
retained. Function calls and declarations use parser-owned exact name spans.
Imported project and stdlib definitions use the existing workspace lock and
physical-input closure to map their logical source unit to the physical `.zhl`
path. Unsupported declaration categories return `None` rather than being
reconstructed from names or source text. Resolver and workspace failures remain
`ToolingError`; the projection never exports AST, resolver, or typed-IR objects.

## References

`TOOLING_REFERENCE_SCHEMA` is currently `1`. The
`references_at(source, source_text, line, character, include_declaration)`
function reuses the compiler's occurrence-to-target resolution records and
returns immutable `ToolingReference` records containing only a locked physical
source path and authoritative occurrence origin. Results are grouped by the
compiler-owned declaration origin, never by identifier spelling, and are
sorted deterministically with exact duplicate locations removed.

When `include_declaration` is true, the authoritative target declaration is
included. When false, only resolved usages are returned. Malformed or
unresolved source returns an empty tuple; workspace and source-path failures
remain `ToolingError`. For a saved declaration in a locked project, the tooling
layer considers only project roots whose dependency closure can contain that
declaration. A parser-only name prefilter bounds the expensive work, then each
candidate root is independently checked and only its compiler-owned
occurrence-to-target records may become results. Reported spelling is validated
against the exact source span to reject malformed inherited provenance. The
prefilter is not a reference search and cannot create a result. Unsaved
declaration snapshots remain local so in-memory and disk identities are not
mixed. The persistent normalized shard described below only replays these
compiler-owned records; no spelling-based result fallback, resolver object,
AST, or typed-IR object is exposed.

## Rename

`TOOLING_RENAME_SCHEMA` is currently `1`. The
`rename_at(source, source_text, line, character, new_name)` function resolves
the target through the compiler-owned definition/reference identity and returns
an immutable tuple of `ToolingRenameEdit` records, or `None` when the category
is not rename-safe. Each edit contains a locked local source path, an exact
parser-owned identifier `ToolingOrigin`, and the replacement text.

New names are validated with the parser's ordinary binding-name rule. Before
returning edits, the tooling layer applies them to the current root snapshot
and performs a semantic-only recheck; invalid names, collisions, retargeted
references, missing exact spans, and cross-file edits fail closed with
`ToolingRenameError`. The initial supported categories are local ports,
immutable values, functions, and function parameters. Registers, instances,
types, protocol members, and cross-file rename are intentionally unsupported.
The LSP converts these records to the standard `WorkspaceEdit.changes` form;
the tooling API does not expose AST, IR, resolver objects, or LSP classes.

## Completion

`TOOLING_COMPLETION_SCHEMA` is currently `1`. The
`completion_at(source, source_text, line, character)` function consumes
compiler-recorded semantic scope snapshots and returns immutable
`ToolingCompletion` records. The projection currently covers visible ports,
immutable values, function parameters, ordinary/generic functions, and
functions imported through the locked workspace. It does not reconstruct
scopes, scan source text, add imports, or provide keyword/snippet completion.

Completion uses only the semantic check product. Malformed or incomplete source
and positions outside an analyzed expression return an empty tuple; workspace
and resolver failures remain `ToolingError`. The API does not expose the
semantic scope, AST, resolver, or typed-IR objects themselves.

## Signature help

`TOOLING_SIGNATURE_HELP_SCHEMA` is currently `1`. The
`signature_help_at(source, source_text, line, character)` function returns
either an immutable `ToolingSignatureHelp` record or `None`. The record
contains one compiler-resolved callable label, its parameter labels, the
active parameter index, and the authoritative call origin.

The semantic analyzer records a call only after ordinary, generic, or locked
imported function resolution succeeds. Argument origins are retained by the
compiler and are used to select the active parameter; the tooling and LSP
layers do not parse commas, parentheses, or identifier text. Nested calls are
resolved by choosing the most-specific compiler call span. Malformed or
incomplete source and positions outside a resolved call return `None`, while
workspace/environment failures raise `ToolingError`. Signature help demands
only the semantic snapshot and does not run optimization, implementation
selection, RTL generation, synthesis, formal tools, or external processes.

The projection deliberately exposes no AST, typed IR, resolver state, generic
substitution machinery, or implementation metadata. Trigger characters are
not advertised by the initial LSP slice because requests are handled
identically regardless of how they are triggered.

## Semantic tokens

`TOOLING_SEMANTIC_TOKEN_SCHEMA` is currently `1`. The
`semantic_tokens(source, source_text)` function returns immutable
`ToolingSemanticToken` records for exact compiler-owned declaration and
resolved-occurrence spans in the current root document. Tooling categories are
protocol independent: `function`, `parameter`, `property` for ports, and
`variable` for immutable values. Declarations carry the `declaration` modifier;
usages do not.

The projection consumes the same semantic-only snapshot and exact identifier
origins used by definition, references, and rename. It rejects broad or
multiline spans, filters imported declarations out of the root document,
orders records deterministically, removes exact duplicates, and fails closed
on overlap. It does not classify source text, expose a lexer/AST/IR object, or
assign numeric LSP legend indices. Malformed or semantically invalid source
returns an empty tuple; project/environment failures remain `ToolingError`.

## Reusable tooling and symbol sessions

`ToolingSession` is the bounded, request-driven cache used by the LSP for
repeated tooling queries. It stores immutable `SemanticCheckResult` products
for at most eight root-document contexts; standalone tooling calls remain
unchanged when no session is supplied. A context key contains the resolved
root path, the SHA-256 digest of the exact in-memory source text (plus any
non-matching caller assertion), project/profile/top selection, the tooling API
and compiler versions, and the requested `AnalysisNeeds` tracked on the
cached entry.

The semantic cache reuses an entry only when its recorded needs are a superset
of the request. A weaker request is a hit; a stronger request rechecks with the
safe union of needs and replaces the entry. The compiler's
`PhysicalCompilationInputs.all_paths` provide the authoritative locked root,
dependency, manifest, lock, standard-library and external-source closure.
Session-local file signatures are checked on each request and content hashes
are recomputed only after a signature change, so changed dependencies cannot
reuse a stale result without introducing directory polling or a workspace
index. The session only memoizes the existing locked workspace projection; it
does not create a new workspace-wide semantic index.

Definition and References have a second, normalized cache whose entries retain
only deduplicated `DefinitionTarget`/`DefinitionResolution` origins and stable
declaration identities. Each loaded shard builds a source-line interval index
for cursor lookup and a declaration-to-occurrences index for References. It
does not retain source text, AST, typed IR or generated artifacts. The in-memory
limit is 64 shards or 64 MiB and remains useful after the eight-entry semantic
LRU evicts a root.

Because a shard is published only after a successful definition-aware compiler
analysis, an exact content/recipe hit may also serve semantic tokens and prove
that the identical root snapshot has no diagnostics. The LSP can reuse a parent
root shard after module-definition navigation only for the exact selected
module, logical source unit and source digest recorded by the compiler. It does
not use this path for arbitrary manually opened files.

For saved locked-project roots, the same normalized payload is published
atomically below `$XDG_CACHE_HOME/zlang-hdl/lsp/symbol-v1/` (or
`~/.cache/zlang-hdl/lsp/symbol-v1/`). A new `ToolingSession`, including one in a
restarted LSP process, may reuse it only after validating the schema,
compiler/capability identity, root/profile/top recipe, manifest and lock, and
the exact content hashes of its logical project/dependency/stdlib units.
Physical paths are reconstructed through the current resolver and never stored
in JSON. Unsaved and standalone documents remain memory-only. Corrupt,
oversized or symlink shards are discarded or ignored and the compiler path is
used normally.

`ZLANG_LSP_SYMBOL_CACHE` selects `persistent` (default), `memory`, or `off`.
TTL is never validity evidence: access time is used only for daily-throttled
usage updates and 30-day/count/size garbage collection (512 shards/256 MiB).
Timestamp-only changes preserve a hit when bytes match, while a changed used
dependency, manifest, lock, schema or compiler identity misses. An unrelated
project file does not participate in the shard's physical input set.
`didChange` and replacement `didOpen` explicitly invalidate the affected
in-memory root. `didClose` drops the editor buffer but retains bounded
content-addressed semantic and symbol entries; all later reuse still validates
the current bytes and environment. No background work, source-text/regex result fallback,
incremental compiler, optimizer, backend, synthesis or formal phase is
involved.
