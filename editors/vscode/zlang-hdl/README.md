# ZLang HDL for Visual Studio Code

Lightweight Community editing support for ZLang HDL `.zhl` files:

- syntax highlighting for types, generics, state, protocols and verification;
- line/block comments, bracket matching, auto-closing and indentation;
- eight concise snippets, with defaults checked by the ZLang compiler;
- ordinary indentation-based folding.

The extension also starts the existing Community `zlang-lsp` over standard stdio.
Diagnostics, symbols, hover, navigation, references, rename, completion,
signature help, semantic tokens and compiler-owned quick fixes come from that
server through the standard VS Code language-client library. The extension does
not contain a second parser, type checker, semantic provider, compiler or AI
service. Lexical highlighting remains useful when the server is unavailable;
server-backed facts are not inferred from TextMate scopes.

## Identity and appearance

Extension ID: `postoroniy.zlang-hdl`; language ID: `zlang-hdl`.
Only `.zhl` is registered. The existing TextMate scope `source.zlang` is retained
for theme compatibility; it does not register a `.zlang` or `.zl` file type.

Your theme controls colors. Optional `recommended-settings.json` retains the
project's three function styles: normal user calls, bold hardware/conversion
intrinsics, italic compile-time intrinsics. It also offers red operators and
matching yellow signal definitions/references. Copy its
`editor.tokenColorCustomizations` rules into your settings if desired; the
extension never rewrites user or workspace settings. Angle brackets are not
auto-closing pairs, so comparisons and arrows remain complete operator tokens.

## Snippets

Use **Insert Snippet** or type a prefix and select its suggestion. Descriptions
state the required insertion scope and signals.

| Prefix | Inserts |
| --- | --- |
| `zmodule` | Clocked module with typed pass-through |
| `zfn` | Pure function with inferred return type |
| `zreg` | Reset-initialized register |
| `zwhen` | Named atomic `when` rule |
| `zpipeline` | Explicit fixed-depth pipeline expression |
| `zassert` | Named verification assertion |
| `zcontract` | Scoped requirement and public-output guarantee |
| `zrv` | Ready/valid ports and unbuffered connection |

## LSP server configuration

The client resolves the server without a shell. Set `zlang.lsp.path` to an
absolute executable path, or to a workspace-relative path such as
`${workspaceFolder}/.venv/bin/zlang-lsp`. If the setting is empty, the client
uses `zlang-lsp` from `PATH`. Missing or non-executable paths are reported in the
`ZLang HDL` output channel with a link to the setting; the extension never scans
for virtual environments, installs packages, or downloads a server.

Because this setting selects a local executable, VS Code activates the extension
only for trusted workspaces. Review a project before granting workspace trust.

The initial client supports local `file://` documents and full-document
synchronization. It deliberately does not add parser recovery, unsaved
dependency overlays, or generated-RTL navigation; the compiler/LSP remain the
semantic authority.

## Build and install locally

From a repository checkout, use Node 22 and the checked-in dependency lockfile.
Build tools are dev-only and are not shipped inside the VSIX.

```sh
cd editors/vscode/zlang-hdl
npm ci --ignore-scripts
npm test
npm run package -- /tmp/zlang-hdl-vscode1.vsix
```

Choose a new output filename; packaging refuses to overwrite an existing VSIX.
The build uses the official pinned `@vscode/vsce` in an isolated staging
directory containing the approved editor assets, the production
`vscode-languageclient` runtime and copies of the repository's authoritative
Apache-2.0 `LICENSE` and `NOTICE`. Development dependencies, tests, compiler
artifacts and credentials are not shipped. It does not publish, create tags, or
run the compiler.

From the repository root, inspect the actual package and install it:

```sh
.venv/bin/python tests/editor/test_vscode_package.py /tmp/zlang-hdl-vscode1.vsix
code --install-extension /tmp/zlang-hdl-vscode1.vsix
```

Alternatively use **Extensions: Install from VSIX**. Remove any old manually
copied `zlang-hdl` extension before installing, to avoid loading two local copies.
For testing, use separate `--user-data-dir` and `--extensions-dir` directories;
do not replace your everyday profile. Reload the window after an update.

## Validation

From the repository root:

```sh
.venv/bin/python -m pytest -q tests/editor tests/conformance
npm --prefix editors/vscode/zlang-hdl test
```

Tests reuse `examples/all_syntax.zhl` and compiler-owned capability metadata.
Real TextMate/Oniguruma tests cover nested generic calls, complete operators,
contextual identifiers, comments, byte escapes and incomplete editor input.
Python tests compile expanded snippets in their documented contexts. The VSIX
audit verifies its exact inventory, the standard language-client runtime, and
license bytes.
CI performs these checks without publishing credentials.

`test/host-smoke.cjs` is a dev-only VS Code extension-host test: after installing
the VSIX into a fresh temporary profile, set `ZLANG_EDITOR_SMOKE_EXTENSION` to
its installed directory and launch that directory with
`--extensionDevelopmentPath`, `--extensionTestsPath` pointing to the test, and
a fresh temporary workspace. It tests real file recognition, activation-ready
F12 definition and Shift+F12 References, nested locked-project type/module navigation with unopened
targets, comments, bracket indentation and registered snippet insertion.
It is not included in the VSIX.
Rendered colors/fonts still need a visual smoke check with your chosen theme:
open `all_syntax.zhl`, inspect `>=`, `<-`, `->`, `@`, `SF8.8`, `pipeline`, signal
uses and comments, using **Developer: Inspect Editor Tokens and Scopes**. For
LSP dogfooding, use the checklist below and inspect the `ZLang HDL` output
channel when starting or stopping the server.

## Dogfooding checklist

1. Build and audit the VSIX, then install that exact file in an isolated VS Code
   profile.
2. Make `zlang-lsp` available on `PATH`, or set `zlang.lsp.path` to the local
   executable in the workspace `.venv`.
3. Open a real `.zhl` file and confirm diagnostics update after edits; check the
   output channel for the server command and startup failures.
4. Exercise compiler-backed hover, document symbols, F12 definition, Find All
   References (including a local, port, module and imported type), rename,
   completion, signature help, semantic tokens and the duplicate-import quick
   fix. Confirm the extension itself has no provider fallback.
5. Repeat with an unsaved edit and a deliberately malformed buffer. The server
   should publish diagnostics and safely return empty/unsupported results where
   compiler facts are unavailable.

The first F12/References lookup for a saved project root performs normal
semantic analysis. Later lookups reuse a normalized compiler-symbol shard,
including after the heavy semantic LRU evicts the root or the LSP restarts.
Persistent shards use the platform XDG cache, never the source tree. Set
`ZLANG_LSP_SYMBOL_CACHE=memory` for memory-only operation or `off` to disable
the symbol cache while debugging; content identities, not elapsed time, decide
whether a shard is reusable.

This first client is intentionally local and conservative: it uses full
document sync, has no parser-recovery layer or unsaved dependency overlay, and
does not consume the generated-artifact/navigation bundles from later work.

## Publication — maintainer action only

This is local release preparation, not a registry publication. `postoroniy` is
the existing publisher candidate; prove control of it in **both** registries
before the first upload. Keep compiler and extension versions independent.
Version 0.1.0 follows the repository-local 0.0.9; it is not a compiler version.

1. Review the exact source commit, tests, VSIX inventory and recorded SHA256.
2. Confirm/create the Marketplace publisher and Open VSX namespace yourself.
3. Publish the **same inspected VSIX**, not a fresh source rebuild, only after
   explicit authorization. Do not use version-bumping publish commands.
4. Confirm listing identity/version and install from each registry afterwards.

For Marketplace, upload the VSIX through publisher management, or use the
pinned `vsce publish --packagePath /absolute/package.vsix` after configuring
publisher authentication. Prefer Microsoft's current identity-based publishing
guidance for future automation; PR CI never receives publishing credentials.
See [Microsoft's publishing guide](https://code.visualstudio.com/api/working-with-extensions/publishing-extension).

Open VSX requires an Eclipse account, its Publisher Agreement, a namespace and
an access token. Namespace creation and verified ownership are separate steps.
Once authorized, `npx --package ovsx@1.1.1 ovsx publish /absolute/package.vsix`
can upload the reviewed file using `OVSX_PAT` supplied through a secure secret
mechanism. Never commit tokens or put them in command-line arguments.
See [Open VSX publishing](https://github.com/eclipse-openvsx/openvsx/wiki/Publishing-Extensions).

No GitHub Linguist submission or other editor port is part of this release.
Support and issues: [ZLang HDL](https://github.com/postoroniy/zlang-hdl/issues).
Copyright 2026 Viacheslav Vinogradov; Apache-2.0. The packaged extension includes
the project license and attribution notice.
