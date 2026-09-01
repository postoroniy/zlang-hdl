# Generated source maps

ZLang backends can publish a deterministic JSON sidecar alongside an immutable
`BackendArtifact`.  The sidecar is backend-independent: each entry connects an
inclusive generated line range and semantic identity to the complete typed
`SourceOrigin` (`source_unit`, digest, span, and construct).  It also records the
backend, module, selected-IR identity, and generated artifact SHA-256, so a map
cannot silently be applied to different generated text.

The schema is version 1 and is implemented by
`zlang.backend.source_map.GeneratedSourceMap`.  Clash and direct SystemVerilog
offer `emit_artifact_with_source_map`; this returns the unchanged production
artifact plus its sidecar model.  `write_sidecar` writes canonical, sorted JSON.
The CLI exposes the same model for exactly one explicit backend output:

```bash
zlangc design.zl -o Design.hs --source-map Design.hs.zmap.json
zlangc design.zl --systemverilog Design.sv \
  --source-map Design.sv.zmap.json
```

The sidecar artifact hash must match the generated file before a consumer uses
any line attribution.

The external-tool helper applies the same rule to Clash/Verilator messages: it
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
Clash maps the unique simple `topEntity ... = ...` equation.  Ambiguous,
sequential, multi-output, hierarchical, protocol, and backend-generated helper
lines remain deliberately unmapped.  No mapping is inferred from similar source
and RTL/Haskell names.  Later emitter refactoring may attach exact origins while
fragments are constructed; until then, absence of an entry means “unknown,” not
“same as the nearest mapped line.”
