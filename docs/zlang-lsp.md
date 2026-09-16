# `zlang-lsp` Community language server

`zlang-lsp` is the single Community ZLang HDL language server. Its protocol
surface provides deterministic diagnostics, document symbols, semantic hover,
compiler-resolved definition locations, semantic references, safe semantic
rename, deterministic semantic completion, and compiler-resolved signature
help, full-document semantic tokens, and compiler-owned diagnostic quick fixes.
It keeps open document text in memory and queries the compiler-owned tooling
projections after full-document opens and changes.

Start it as a standard LSP process:

```sh
.venv/bin/zlang-lsp
```

The process reads and writes standard Content-Length framed JSON-RPC messages.
It supports local `file://` URIs for existing `.zhl` files. The server does not
write editor buffers to temporary source files; project-backed snapshots must
therefore remain compatible with the existing compiler/tooling workspace
identity rules. Unsupported remote, virtual, and non-local document URIs are
reported explicitly.

The project has no existing JSON-RPC/LSP dependency. This slice therefore uses
the Python standard library for the small Content-Length framing and dispatch
surface instead of adding a general RPC framework or a large LSP dependency.

## Supported now

- `initialize` / `initialized`;
- `shutdown` / `exit`;
- `textDocument/didOpen`;
- `textDocument/didChange` using full-text synchronization;
- `textDocument/didClose`;
- `textDocument/publishDiagnostics`;
- `textDocument/documentSymbol` for an open document;
- `textDocument/hover` for an open document;
- `textDocument/definition` for an open document;
- `textDocument/references` for an open document;
- `textDocument/rename` for the conservative rename-safe subset of an open
  document.
- `textDocument/completion` for compiler-visible expression-scope candidates.
- `textDocument/signatureHelp` for one compiler-resolved callable at the
  requested position.
- `textDocument/semanticTokens/full` for exact compiler-classified identifiers.
- `textDocument/codeAction` for compiler-owned machine-applicable quick fixes.

Diagnostics use the Community `zlang.tooling.check_snapshot()` API, which uses
the normal parser, resolver, and semantic checker. They keep the compiler code,
message, source origin, and one-based-to-zero-based range conversion. Document
symbols use the narrow parser-owned `zlang.tooling.document_symbols()`
projection over the current unsaved editor text. Hover uses the narrow semantic
`zlang.tooling.hover_at()` projection over the same current text and source
path. The LSP does not contain a second parser, type checker, workspace model,
backend, formal runner, or optimizer.

Editor observations are demand-driven through one compiler-owned
`AnalysisNeeds` mask. Definition, references, rename, and semantic-token
requests collect only shared definition records; completion additionally
requests scope candidates, and signature help requests resolved-call records.
A definition request therefore does not build completion scopes or callable
detail strings, while semantic checking and the resulting IR remain unchanged.

Document symbols are structural declarations, not a semantic database. Their
names, categories, containment, and available source spans come from the
compiler's parser representation. Incomplete or malformed text returns an
empty symbol list while diagnostics continue to report the parse error; there
is no regex or source-text fallback. A declaration whose AST node has no
dedicated source origin uses the enclosing authoritative declaration span.

Hover reports only compiler-owned facts that are already available from the
semantic IR: the selected name/kind, canonical type, width, signedness,
fixed-point representation, and (for ports) direction. Function declarations
may include their compiler-owned signature. It does not run optimization,
implementation selection, RTL generation, synthesis, or formal tools. A
malformed/incomplete buffer or a position without a semantic entity returns
`null`; workspace/environment failures remain explicit protocol errors.

Definitions use `zlang.tooling.definition_at()` and the compiler's actual
name/import resolution. The server does not search source text, rebuild
scopes, or choose same-named declarations. Project imports are mapped through
the locked workspace to the target source URI; unresolved or unsupported
categories return `null`.

For a file declaring several modules, Definition and References share one
bounded navigation-context selector. A missing default-top occurrence is
retried under the module preceding the cursor according to parser-owned module
declaration spans. Only that selected module is then semantically analyzed;
the compiler's exact occurrence-to-declaration record still decides whether
F12 returns a location. A bounded session-local, exact-text selector avoids
rechecking an unrelated generic last module on repeated F12; no source AST or
physical path is added to persistent symbol shards.

Protocol connection endpoints retain exact parser-owned name spans. Each name
is resolved by semantic elaboration, so a connection such as
`command -> packet_mapper.command` can navigate independently to the parent
port declaration, the instance declaration, and the selected child port. The
cache stores only those compiler-produced relations.

VS Code may issue F12 at the exclusive right edge of its selected identifier.
Tooling performs the exact half-open-span lookup first, then retries the
immediately preceding code point only when the cursor is at an identifier
boundary. It does not walk across whitespace or punctuation. Declaration paths
outside the root project, including stdlib targets, are returned only when they
are members of the compiler-owned locked physical input closure.

Definition coverage includes compiler-resolved module instance targets, named
user types (including nested generic components, structs and aliases), nominal
enum types, enum members, and local registers. Targets retain their locked
source identity, digest, and parser-owned declaration span, so a dependency
file need not be open in the editor. Builtin constructors such as `rv` and
`bits` have no fabricated source definition.
Source-local target/resource declarations also retain exact lexer-token name
spans: `provides ResourceName` and `require_resource ResourceName` navigate to
one unambiguous `resource ResourceName` declaration and participate in semantic
References. Unknown or duplicate source-local names return no location. This
does not yet implement general cross-file target-catalog navigation; physical
target legality remains with the separate target planner.
The server converts VS Code's incoming UTF-16 character offsets to compiler
code-point columns before semantic span containment; outbound locations use the
inverse conversion.

The VS Code client awaits complete `LanguageClient.start()` registration in
its extension activation promise. This guarantees that an immediate first F12
request is not dispatched before the server's definition provider exists; it
does not add any extension-side symbol lookup or project semantics.

Generic declarations are semantically checked for their concrete
specializations. When a generic-only library file is opened directly, the
compiler's dedicated `ZL-GENERIC-SPECIALIZATION-REQUIRED` condition is not
published as an editor error. A parameter constraint referencing a declared
value parameter without a default is likewise deferred only while directly
viewing its unspecialized template; concrete false constraints and unrelated
runtime references remain editor errors. The production compiler still rejects
an unspecialized top, and parse/concrete-specialization errors remain ordinary
diagnostics.

A saved project child opened directly is checked using the semantic analyzer's
existing child-boundary mode. This avoids treating an enum-bearing internal
port as an illegal public top ABI merely because its parent is not the active
editor document. The production compiler's default top-level boundary remains
fail-closed; all other child semantics and concrete imported types are checked.

References use `zlang.tooling.references_at()` and the same compiler-owned
occurrence-to-target relation. Results are grouped by resolved declaration
identity, not by spelling, and may include the declaration when the LSP
`includeDeclaration` flag is true. For a saved locked-project snapshot, the
tooling layer builds an on-demand bounded view of dependency-compatible project
roots and accepts only compiler-resolved occurrences with an exact source span.
The project sweep reloads the current manifest/lock and reuses each exact root
text for parser filtering and semantic checking. It verifies disk bytes before
publication; a concurrent edit or more than 128 eligible roots/top pairs
reports an error instead of a stale or silently truncated result.
A declaration above several modules in one source also checks bounded sibling
tops that may use it; this includes encoded-enum uses inside a non-default
module. A parser prefilter limits semantic checks but never produces a reference
result. Qualified enum owners and members retain separate identifier spans, and
incoming LSP UTF-16 cursor columns are converted before compiler lookup.
The normalized symbol cache described below reuses compiler-produced shards;
it does not create results. There is no text/regular-expression fallback, and
an unsaved declaration snapshot is never mixed with disk roots.

Rename uses `zlang.tooling.rename_at()` and the same compiler-owned identity and
occurrence records. It edits only exact parser-owned identifier spans, always
including the declaration, and rechecks the edited in-memory root source with
the semantic checker to reject collisions or retargeted references. The first
slice supports local ports, immutable values, functions, and function
parameters. Cross-file edits and declarations without exact editable spans are
rejected; there is no textual or same-spelling fallback.

Completion uses `zlang.tooling.completion_at()` and semantic scope snapshots
recorded by the compiler while checking the current source. It offers only
visible ports, immutable values, parameters, ordinary/generic functions, and
locked imported functions. Candidates are deterministic, insert the plain
identifier, and carry optional type/signature detail. The server does not
reconstruct scopes, scan source text, synthesize imports, or provide keyword
or snippet completion. Positions outside a compiler-analyzed expression and
malformed/incomplete buffers return an empty list.

Signature help uses `zlang.tooling.signature_help_at()` and call records
captured while the compiler resolves ordinary, generic, and locked imported
function calls. The callable label, parameter labels, return type, call span,
and argument spans are compiler-owned. The server selects the most-specific
resolved call containing the cursor and determines `activeParameter` from
those spans; it never counts commas or parses call text itself. A cursor in
malformed/incomplete source, or outside a resolved call, returns `null`.
No trigger characters are advertised until parser behavior makes a trigger
contract useful.

Semantic tokens use `zlang.tooling.semantic_tokens()` and only the exact
identifier spans already retained by compiler definition/reference records.
The initial categories are functions, parameters, ports, and immutable values;
declarations carry the standard `declaration` modifier and resolved usages do
not. The server publishes a fixed standard-token legend, sorts tokens by source
position, converts compiler code-point columns to the default LSP UTF-16 code
units, and emits standard relative full-document encoding. It does not lex,
scan, classify keywords, infer token roles from spelling, or expose hardware
resource information. Malformed source produces an empty token data array.

Code actions recheck the exact current in-memory source through
`zlang.tooling.check_snapshot()` and map only
`ToolingDiagnostic.machine_fixes`. The current action is the compiler-owned
deletion of a later completely identical duplicate import. Actions are
`quickfix` edit-only `CodeAction` values, filtered by the request range,
`context.diagnostics`, and `context.only`; stale diagnostics and fixes are not
returned. Compiler-native spans are converted to LSP UTF-16 positions without
changing replacement text. Prose-only diagnostic suggestions never become
executable actions.

The server owns one bounded `zlang.tooling.ToolingSession` for the lifetime of
its open-document manager. Its heavy semantic LRU retains at most eight typed
snapshots. Definition and References additionally use a normalized symbol-only
cache: up to 64 shards/64 MiB in memory and, for exact saved project snapshots,
up to 512 shards/256 MiB on disk. A shard contains only deduplicated declaration
and occurrence origins, stable declaration IDs and content identities; loading
it builds line-interval and declaration-to-occurrences indexes. A validated
successful shard may also supply semantic-token records and prove that
diagnostics were already checked for the identical root snapshot. It contains
no source body, AST, typed IR, generated artifact or absolute physical path.

Persistent shards live below
`$XDG_CACHE_HOME/zlang-hdl/lsp/symbol-v1/`, falling back to
`~/.cache/zlang-hdl/lsp/symbol-v1/`. Set `ZLANG_LSP_SYMBOL_CACHE=memory` to
disable disk writes, or `off` to use only the eight-entry semantic LRU;
`persistent` is the default. Publication uses a private temporary file and
atomic rename. Cache corruption, unsafe symlinks and read-only filesystems fall
back to ordinary semantic analysis rather than breaking navigation.

Correctness never depends on age. Reuse requires the exact compiler/tooling
schema and version, root text digest, selected profile/top, nearest project
manifest and lock identities, and the content digests of the transitive project
and stdlib sources used by the symbol records. A timestamp-only change is
accepted after hashing; an unrelated project-source edit is irrelevant.
`didChange` and a replacement `didOpen` immediately invalidate the old
in-memory root snapshot. `didClose` removes the editor buffer but retains its
bounded content-addressed LRU entries: reopening an unchanged VS Code preview
revalidates source/dependency metadata and reuses them, while changed bytes or
dependencies miss safely. Unsaved and standalone buffers are memory-only.
Disk access time is updated at most daily; entries older than 30 days and then
least-recently-used entries beyond the count/byte limits are garbage-collected.
The session performs no background refresh, project-global source scan,
incremental compilation, backend or formal work.

For module-definition navigation, VS Code normally opens the destination file
and immediately requests diagnostics and semantic tokens. The server carries
the selected module name across that one navigation. It may reuse the parent
shard for the destination only when compiler records declare that exact module
in the exact logical source unit with the current on-disk digest. This avoids a
second project analysis without turning an arbitrary parent shard into proof
for a manually opened file. A changed destination, an unsaved buffer or a shard
that did not analyze the selected module falls back to normal semantic analysis.

The generated-RTL provenance audit added no LSP method or capability. Existing
source maps are hash-bound and exact for their few mapped
generated lines, but they do not yet provide complete artifact discovery,
current-editor-source staleness validation, or broad RTL coverage. The LSP does
not load raw backend manifests, infer mappings from generated names, or generate
RTL during navigation. See [Generated source maps](generated-source-maps.md).

generated-navigation bundle adds a protocol-neutral
[validated generated-navigation bundle](generated-navigation-bundles.md) for
explicit published artifact discovery, integrity, lineage, and source-snapshot
freshness. No LSP method consumes the bundle yet, and advertised server
capabilities are unchanged.

## Not implemented

`codeAction/resolve`, fix-all/source/refactor actions, semantic-token
range/delta requests, workspace symbols, generated-RTL navigation,
formal/synthesis commands, AI-assisted workflows, CUDA, and `zinfer` are outside
the current public scope.
