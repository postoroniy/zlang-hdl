<a id="reference-generated-source-maps"></a>
# Generated source maps


ZLang backends can publish a deterministic JSON sidecar alongside an immutable
`BackendArtifact`. The sidecar is backend-independent: each published entry
connects an inclusive generated line range and semantic identity to a complete
typed `SourceOrigin` (source unit, digest, span, and construct). The base
origin record permits an absent source unit or digest, but such an incomplete
record cannot establish fresh cross-file attribution. The sidecar also records the
backend, module, selected-IR identity, and generated artifact SHA-256, so a map
cannot silently be applied to different generated text.

The sidecar format is implemented by
`zlang.backend.source_map.GeneratedSourceMap`. Direct-SV offers
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
[validated generated-navigation bundle](generated-navigation-bundles.md#reference-generated-navigation-bundles) now
provides the missing explicit file/source-snapshot integrity boundary. It does
not expand this map's bounded line coverage and does not add an LSP navigation
method.

The external-tool helper applies the same rule to Verilator messages: it
adds a `ZLang origin:` line only for one exact mapped generated line after hash
validation. Changed files, malformed locations, ambiguous entries, and unmapped
helper/state-machine lines retain the original tool diagnostic unchanged.

<a id="reference-generated-source-maps-exactness-boundary"></a>
## Exactness boundary

The current generated-source-map implementation maps only a simple top-level output assignment
when all of the following are true:

- the typed output expression retains a complete `SourceOrigin`;
- the `BackendArtifact` publishes the matching semantic output binding;
- the emitter's generated assignment statement is uniquely identifiable; and
- the generated text hashes to the artifact identity.

Direct-SV maps a unique `assign <published-token> = ...` statement.
Ambiguous, sequential, multi-output, hierarchical, protocol, and backend-generated helper
lines remain deliberately unmapped.  No mapping is inferred from similar source
and RTL names. Later emitter refactoring may attach exact origins while
fragments are constructed; until then, absence of an entry means “unknown,” not
“same as the nearest mapped line.”

<a id="reference-generated-source-maps-generated-rtl-navigation-audit"></a>
## Generated-RTL navigation boundary

A generated source map gives exact generated **line** ranges, not columns. Only
a unique top-level output assignment with a retained complete compiler
`SourceOrigin` is currently mapped; the final output assignment of an eligible
pipeline may also qualify. Module declarations, internal state, helpers,
protocol bridges and hierarchy are otherwise unmapped. An origin's span and
construct are always present; its logical source unit and digest are optional
in the base record, but a complete mapped origin has all four.

A consumer must first verify the generated text against the artifact SHA-256.
Zero matching entries means “unmapped”; multiple distinct complete origins are
ambiguous and must not be guessed from names or proximity. The bundle can
validate source-snapshot freshness, but there is no supported
source-to-generated query or LSP navigation method. Generated coordinates are
one-based lines; source spans use compiler-native code-point columns, converted
to UTF-16 only by an LSP client boundary. See the
[generated-navigation bundle](generated-navigation-bundles.md#reference-generated-navigation-bundles) for
publication and integrity details.
