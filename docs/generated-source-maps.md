# Generated source maps

ZLang backends can publish a deterministic JSON sidecar alongside an immutable
`BackendArtifact`.  The sidecar is backend-independent: each entry connects an
inclusive generated line range and semantic identity to the complete typed
`SourceOrigin` (`source_unit`, digest, span, and construct).  It also records the
backend, module, selected-IR identity, and generated artifact SHA-256, so a map
cannot silently be applied to different generated text.

The schema is version 1 and is implemented by
`zlang.backend.source_map.GeneratedSourceMap`. Direct SystemVerilog offers
`emit_artifact_with_source_map`; this returns the unchanged production artifact
plus its sidecar model. `write_sidecar` writes canonical, sorted JSON. The CLI
exposes the same model for one explicit production output:

```bash
zlang design.zhl --systemverilog Design.sv \
  --source-map Design.sv.zmap.json
```

The sidecar artifact hash must match the generated file before a consumer uses
any line attribution.

For published consumers, the versioned
[validated generated-navigation bundle](generated-navigation-bundles.md) now
provides the missing explicit file/source-snapshot integrity boundary. It does
not expand this map's bounded line coverage and does not add an LSP navigation
method.

The external-tool helper applies the same rule to Verilator messages: it
adds a `ZLang origin:` line only for one exact mapped generated line after hash
validation. Changed files, malformed locations, ambiguous entries, and unmapped
helper/state-machine lines retain the original tool diagnostic unchanged.

## Exactness boundary

The first bounded implementation maps only a simple top-level output assignment
when all of the following are true:

- the typed output expression retains a `SourceOrigin`;
- the `BackendArtifact` publishes the matching semantic output binding;
- the emitter's generated assignment statement is uniquely identifiable; and
- the generated text hashes to the artifact identity.

Direct SystemVerilog maps a unique `assign <published-token> = ...` statement.
Ambiguous, sequential, multi-output, hierarchical, protocol, and backend-generated helper
lines remain deliberately unmapped.  No mapping is inferred from similar source
and RTL names. Later emitter refactoring may attach exact origins while
fragments are constructed; until then, absence of an entry means “unknown,” not
“same as the nearest mapped line.”

## Generated-RTL navigation audit

The Community LSP M12 audit does not add an editor navigation API. The current
source-map evidence is safe for exact diagnostic attribution, but it is not yet
complete enough to be presented as general bidirectional generated-RTL
navigation.

The existing ownership and records are:

- `GeneratedLineRange` is a one-based inclusive generated **line** range. It
  contains no generated columns.
- `GeneratedSourceMapEntry` joins that range to one semantic identity and one
  full `SourceOrigin`. The source origin contains a one-based half-open source
  span, logical source unit, construct label, and optional source SHA-256.
- `GeneratedSourceMap` identifies one backend/module/selected-IR/artifact-hash
  tuple. Schema version 1 sorts entries canonically and preserves overlapping
  proven entries; `entries_for_line()` returns every entry covering a line.
- `BackendArtifact` owns the emitted text, its SHA-256, selected-IR identity,
  signal bindings, recursive component/instance/binding manifests, companion
  files, timing, dependency, signature, physical-domain, and implementation
  metadata. Those bindings are not generated line locations.

### Proven granularity

| Emitted category | Current mapping precision |
| --- | --- |
| One unique direct-SystemVerilog top-level output assignment in a module with exactly one assignment and one output | **Exact generated line**, with the typed expression's authoritative source origin |
| Pipeline result's final unique output assignment when it meets that same restriction | **Exact final assignment line only**; internal pipeline registers and sequential block are unmapped |
| Module and port declarations | **Unmapped** |
| Multiple output assignments | **Unmapped** by the current bounded builder |
| State/register declarations and sequential blocks | **Unmapped** |
| Functions/helpers and generated temporary signals | **Unmapped** |
| Protocol lowering and public aggregate wrappers | **Unmapped** |
| Child modules, instances, and recursive hierarchy | **Unmapped as generated lines**; recursive manifest identities/bindings remain available separately |
| Formal/backend scaffolding and helper code | **Unmapped** |

The current builder has no construct-level or coarse navigation entries: an
entry is an exact generated statement line, otherwise it is omitted. Source
spans may identify an expression rather than an identifier token, and must not
be described as exact identifier navigation.

The recursive manifest schemas have optional `source_origin` fields, but the
current direct-SystemVerilog hierarchy publication does not populate them for
ordinary component instances or recursive signal bindings. Even when populated,
their physical signal paths would still not prove generated text line ranges.

### Directional capability

Generated-to-source lookup is bounded but authoritative after a consumer has
already obtained the matching map and generated text: verify the generated
text SHA-256 against `artifact_hash`, call `entries_for_line()`, and preserve
all returned origins. Zero entries means unmapped. Multiple entries are an
explicit ambiguity; existing external-diagnostic attribution fails closed
unless they reduce to one complete semantic origin. `source_unit` and digest
participate in that decision; equal rendered spans from distinct source
snapshots are not merged.

Source-to-generated lookup has no supported query API. A consumer can observe
source origins in entries, but the schema has no source index, physical source
path, generated artifact path, or artifact-discovery contract. One source
construct may eventually map to multiple generated ranges, so a future API
must preserve a collection rather than select a first match.

### Artifact identity and staleness

The generated side is strongly bound: `artifact_hash` is the SHA-256 of the
exact generated text, and the map also repeats backend, module, and
selected-IR identity. A changed `.sv` file is rejected by existing consumers.

Each mapped `SourceOrigin` normally carries the digest of the source snapshot
that produced it, so a future query can reject changed editor text for that
entry. The M13 bundle loader now validates caller-supplied current source text
or digest against complete bundle-level snapshots, including the root when a
map has zero entries. There is still no source-to-generated location query or
LSP method.

The sidecar itself contains no physical `.sv` path. The CLI writes generated
RTL and a sidecar only when explicitly requested; `BackendArtifact` and
`GeneratedSourceMap` can otherwise exist only in memory. A whole-build manifest
can bind published products by logical path and content hash. The separate M13
bundle now gives future tooling one explicit relocatable publication root and a
fail-closed loader; navigation must still never generate RTL implicitly.

The serialized `BackendArtifact` manifest intentionally does not embed the RTL
text: restoration retains the hash and binding metadata with an empty `text`
field. A consumer must therefore obtain and hash the separately published RTL
file. `BackendBuildRecord` can identify the RTL and source-map publications by
content hash and relocatable logical path inside a validated whole-build file
map, but no tooling API currently owns that physical-path resolution.

Generated coordinates make no character-encoding promise because version 1
contains no generated columns or lengths. Source coordinates remain compiler
native one-based code-point spans. Artifact hashes are computed from the
in-memory generated text encoded as UTF-8 by Python's default `str.encode()`;
future LSP UTF-16 conversion remains a protocol-layer concern.

### Published provenance prerequisite

The versioned, hash-validated
[generated-navigation bundle](generated-navigation-bundles.md) now binds a
relocatable generated file, this exact sidecar, the canonical backend manifest,
and complete source snapshot identities, including the root when this map has
zero entries. A later reviewed slice may project protocol-neutral source to
generated locations over a validated loaded bundle. Generated line coverage
remains independent and can be expanded only at emitter-owned fragment
construction points; names, comments, or proximity are never substitutes for
provenance.
