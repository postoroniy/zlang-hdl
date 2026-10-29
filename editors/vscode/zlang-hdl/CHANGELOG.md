# Changelog

## Unreleased

- Add all supplied runnable example launches and a live-waveform Switch debug
  picker; terminate the previous active ZLang session before replacement and
  use the current file's configured top, stimulus and workspace folder.

- Add the ready-to-run CORDIC launch, accept typed structured/signed JSONL
  inputs through compiler-owned packing, and identify/rebind the live waveform
  when the active debug session changes.

- Fix MSAL/qs build-tool vulnerabilities without downgrading the VSIX packager;
  qualify the language-client 11 upgrade against the pinned editor host.

- Make Shift+F12 open Peek References in ZLang source editors even for one usage,
  in both the universal and Linux-x64 extensions.

- Keep module-body syntax highlighting stable after generic `where` constraints,
  whether the constraint is inline, on a new line or follows a comment.

- Add a Linux-x64 event-level native simulation debugger and live waveform
  panel backed by compiler-owned debug metadata and Rust DAP/waveform tools.
- Add a readonly bounded FST/VCD custom editor while retaining a universal
  editor/LSP-only VSIX for other platforms. The Linux-x64 VSIX carries the two
  reviewed native executables and no Rust source or runtime downloader.
- Keep long enum lists stable in Outline and Sticky Scroll by consuming exact,
  nested compiler-owned member ranges.

## 0.1.0

- Bundle the exact language-client runtime into one deterministic file instead
  of shipping a copied dependency tree, reducing the VSIX to 13 files.
- Require the current stable VS Code line (1.141.0 at this build) and test the
  installed VSIX on that exact host with pinned editor tooling.
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
