# Changelog

## 0.1.0

- Package the existing `.zhl` lexical support and a thin standard-LSP client as
  a self-contained Community VSIX.
- Add concise compiler-checked snippets and real TextMate tokenization tests.
- Preserve `zlang-hdl`, the `source.zlang` theme scope and optional color settings.
- Start the existing `zlang-lsp` over stdio through `vscode-languageclient`,
  with an explicit `zlang.lsp.path` setting or a `PATH` fallback.
- Keep extension activation pending until the language client has registered
  providers, so the first F12 request cannot race server startup.
- Exercise nested locked-project F12 for project-defined types/modules with the
  repository as workspace and unopened declaration targets.
- Require VS Code workspace trust before starting the configured local language
  server executable.
- Let VS Code infer language activation from the `.zhl` contribution instead
  of declaring a redundant `onLanguage` activation event.
- No extension-side semantic providers, telemetry, compiler execution, or
  bundled AI integration.

## 0.0.9

- Repository-local lexical integration, compiler capability checks, three
  function styles and explicit signal/operator scopes.
