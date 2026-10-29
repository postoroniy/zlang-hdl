# ZLang HDL for Visual Studio Code

The Linux-x64 waveform workspace provides a hierarchy browser, grouped signals,
multiple time viewports, A/B cursors, bookmarks, digital measurements, and
portable `.zvw.json` sessions for saved VCD/FST and live debugger traces. The
[waveform workspace guide](https://github.com/postoroniy/zlang-hdl/blob/main/docs/waveform-workspace.md)
explains its controls, sessions, and event-time semantics.

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

The universal VSIX intentionally remains editor/LSP-only. A separate
Linux-x64 package adds the a24 native simulator debugger and waveform
viewer. Those features consume a compiler-owned debug bundle; they do not add
an extension-side parser or type checker. Maintainers keep the detailed
architecture in the repository's `docs/native-debugging.md`.

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

The client supports local `file://` documents and full-document synchronization.
Compiler-owned snapshots include open root-package import buffers; external
dependencies remain locked saved inputs. Diagnostics in closed imports point to
their own files, not the importing module. The compiler/LSP remain the semantic
authority; the extension does not add parser recovery or generated-RTL navigation.

**Shift+F12** opens **Peek References** for the ZLang symbol at the cursor,
including when there is only one resolved usage. Close the list with **Escape**;
the shortcut opens it again. This binding is available in both VSIX variants
and applies to the main ZLang source editor when its reference provider is ready.
Other languages retain their existing shortcuts. A function name and its return
type are separate symbols, including when both occur on one line.

Projects with `zlang.toml` require `zlang.lock`, even with zero dependencies.
Restore a clone's checked-in lock and exact dependency sources/cache. For a new
project, run `zlang lock update --project path/to/zlang.toml` once to create it;
repeat only after resolution inputs change. LSP never fetches or rewrites it.
Manifest/lock creation, changes and deletion automatically trigger a debounced
recheck of open ZLang files. Missing locks and malformed manifests appear in
the **ZLang HDL** output channel; they are not source errors at line 1.

## Build and install locally

From the repository root, `make editor-install` builds a fresh, audited VSIX
from this checkout and installs it with `code --install-extension ... --force`.
Linux x86-64 includes the native debugger/viewer; other hosts get editor/LSP
support only. `make venv` also runs this step on local hosts with `code` on PATH
(not in CI); opt out with `INSTALL_EDITOR=0`. Node.js 22 is required for building.
Set `VSCODE=/path/to/code` to select another CLI executable.

After installation, run **Developer: Reload Window** and close/reopen waveform
tabs. Installing another VSIX does not replace JavaScript in an already open
webview. The extension version alone is not a build identity.

From a repository checkout, use Node 22 and the checked-in dependency lockfile.
The extension supports the current stable VS Code release and later compatible
1.x releases; the checked toolchain currently names VS Code 1.141.0. Build tools
are dev-only and are not shipped inside the VSIX.

```sh
mkdir -p build/tmp
vsix_dir="$(mktemp -d "$PWD/build/tmp/editor-vsix.XXXXXX")"
npm --prefix editors/vscode/zlang-hdl ci --ignore-scripts
npm --prefix editors/vscode/zlang-hdl test
npm --prefix editors/vscode/zlang-hdl run package -- \
  "$vsix_dir/zlang-hdl-0.1.0.vsix"
```

For the Linux-x64 debugger package, the release snapshot already contains the
two reviewed, path-scrubbed native executables. Package those exact bytes with
the platform-specific target:

```sh
make native-editor-audit
npm --prefix editors/vscode/zlang-hdl run package:linux-x64 -- \
  "$vsix_dir/zlang-hdl-0.1.0-linux-x64.vsix"
```

Ordinary users should install the matching prebuilt Linux-x64 VSIX from the
GitHub release instead of rebuilding it. The public repository contains the
executables and their reviewed license inventory, but no Rust implementation
source or private release-host build scripts.

Choose a new output filename; packaging refuses to overwrite an existing VSIX.
The build uses pinned `esbuild` and `@vscode/vsce` in an isolated staging
directory. It emits one minified CommonJS runtime containing the exact locked
`vscode-languageclient` closure, plus generated third-party license notices and
the repository's authoritative Apache-2.0 `LICENSE` and `NOTICE`. It does not
ship `node_modules`, a vendor tree, source maps, development dependencies,
tests, compiler artifacts or credentials. It does not publish or create tags.

From the repository root, inspect the actual package and install it:

```sh
.venv/bin/python tests/editor/test_vscode_package.py \
  "$vsix_dir/zlang-hdl-0.1.0.vsix"
code --install-extension "$vsix_dir/zlang-hdl-0.1.0.vsix" --force
```

Alternatively use **Extensions: Install from VSIX**. Remove any old manually
copied `zlang-hdl` extension before installing, to avoid loading two local copies.
For testing, use separate `--user-data-dir` and `--extensions-dir` directories;
do not replace your everyday profile. Reload the window after an update.

## Native debugger

The Linux-x64 VSIX contributes debug type `zlang-hdl`. It executes the same
native simulation plan and JSONL event schedule as `zlang sim`; the extension
does not invent test vectors. Keep stable project stimulus under
`simulation/`, or put transient/generated schedules under ignored `build/`
output.

The source clone includes `.vscode/launch.json` and
`examples/rule_action.events.jsonl`. After installing the matching native wheel
and Linux-x64 VSIX, open the repository folder, save all `.zhl` files, select
**Debug RuleAction**, and press **F5**. In another workspace, add a matching
launch configuration:

```json
{
  "type": "zlang-hdl",
  "request": "launch",
  "name": "Debug RuleAction",
  "program": "${workspaceFolder}/examples/rule_action.zhl",
  "top": "RuleAction",
  "events": "${workspaceFolder}/examples/rule_action.events.jsonl",
  "logicState": true,
  "strictUninitialized": false,
  "initialRegisters": {},
  "traceSignals": ["enable", "fired", "count"],
  "stopOnEntry": true
}
```

Each JSONL line is one atomic event. It may contain binary `set` assignments,
packed four-state `set_logic` assignments, physical `reset` levels, coincident
clock `edges`, and a positive bounded `repeat`. Instead of `events`, a simple
launch may provide `clock` plus bounded `cycles`; the two forms are mutually
exclusive.

Use **ZLang HDL: Switch Debug Example…** or **Switch debug…** in the live
waveform toolbar to choose among all 25 supplied runnable example/top launches.
The command waits for the previous active ZLang session to terminate, starts the
selected launch at event zero, and opens/rebinds the live waveform automatically.
Other-language and inactive debug sessions are left running. Cancelling the
picker leaves the current session intact. Repeat switches are serialized.

The editor-title **Start Native Simulation Debugger** action selects the
configuration for the current source; files with several tops show a picker.
It uses the configured top and JSONL, rather than assuming a module named Top.
Launch paths resolve against the selected workspace folder in multi-root workspaces.
For a new test, add its top and stimulus or bounded clock/cycles to launch.json.
See the [runnable example inventory](https://github.com/postoroniy/zlang-hdl/blob/main/examples/README.md#switch-runnable-examples-in-vs-code).

The normal Run and Debug dropdown/F5 remains available; Shift+F5 stops a manual
session. Opening a different source alone does not change a running simulation.
The live tab identifies the owning top, and each design's layout is recovered
separately. CORDIC needs 16 unstalled clock edges before a result; Continue runs
the complete supplied schedule, while F10 inspects individual events.

Structured JSONL values and signed/wide inputs are packed by the compiler using
the same typed value and protocol bindings as native simulation. The immutable
prepared schedule is checked against the execution plan before launch; it does
not add another simulation engine. Source and prepared schedules are each
limited to 16 MiB, with the existing one-million-expanded-event limit unchanged.
Install matching compiler and extension updates together.

Click the source gutter or press **F9** on a rule such as `when enable` in
`examples/rule_action.zhl`, then use **F5 / Continue** to stop after it fires.
No `debug.allowBreakpointsEverywhere` setting is required.
Source breakpoints are verified only on compiler-published rules, FSM
transitions, and verification clauses. One step completes one atomic hardware
event. Registers and memories are read-only; inputs may be changed while
paused. Assertion, protocol, strict-U/X, and runtime failures appear as DAP
exceptions. Use **ZLang HDL: Open Live Waveform** to follow the active session.

The complete runnable example, test-vector schema, project launch fields, and
troubleshooting are in
[`docs/native-debugging.md`](https://github.com/postoroniy/zlang-hdl/blob/main/docs/native-debugging.md).

## Waveform viewer

While native simulation is paused, right-click a value or field in **Variables**
or **Watch** and choose **Add to Waveform**. The live panel opens with typed
rows: enums retain their member codes, aggregates expose fields/indices, and
tagged unions distinguish active, inactive and unknown variants. Interfaces
appear as channel groups with individual field directions, not packed structs.
For memory, select individual cells or their fields. Recording starts at the
next event; restart for full history. See the [debugging guide](https://github.com/postoroniy/zlang-hdl/blob/main/docs/native-debugging.md#live-and-saved-waveforms)
for the complete workflow and the raw-metadata boundary of saved VCD/FST files.

Live views have the same row presentation, groups, clipboard, search, cursors,
measurements and split viewports as saved files. **Save Session / Load Session**
also support explicit live layout templates. Workspace autosave restores live
arrangements after reopening, and restart clears samples without discarding
groups or styles. **Refresh** never executes a simulation event. **Add signals**
opens the trace selector; **Export Trace** saves FST/VCD for later inspection.

The Linux-x64 package opens `.fst` and `.vcd` files in the ZLang HDL waveform
editor. FST is preferred for compact native traces; VCD remains supported for
external and formal-tool interoperability. The left pane groups the native
query service's signal catalog by scope;
selecting a signal adds it to the synchronized value and waveform panes. The
canvas draws scalar high/low levels, labelled bus segments and distinct `U` and
`X` states instead of rendering an event/value text list.

- **Fit**, **+** and **-** control the event range.
- Mouse-wheel zooms around the pointer; **Shift+wheel** pans in time.
- Hold the middle mouse button and drag left/right to pan in time or up/down to
  scroll signals; double-click restores the complete trace.
- Click a signal row to select and highlight it. **Change**, its direction
  buttons, and Left/Right navigate changes of that selected signal only.
- Click places cursor A; **Shift+click** places marker B and reports the delta.
- Values may be displayed as hexadecimal, binary, unsigned or signed.
- Signals load lazily, and hierarchy filtering keeps large traces bounded.
- Drag rows to reorder them; named nested groups can be collapsed or renamed.
- Split the plot into as many as four independently zoomed/panned viewports.
- Search exact values, bit patterns, any changes, rising edges, or falling edges.

The horizontal unit is the simulator event index. It is deliberately not
presented as analog time. FST/VCD parsing, U-mask restoration, bounded range
queries, and value search remain in the bundled Rust tool; the webview owns
only viewport and presentation state. GHW is intentionally unsupported.

Waveform Control Protocol v0 automation is opt-in and trusted-workspace only.
Set `zlang.waveform.wcp.enabled` to bind a loopback-only listener; use
`zlang.waveform.wcp.port` for a fixed port or leave it at `0` for an ephemeral
port reported in the ZLang HDL output channel.

## Validation

From the repository root:

```sh
.venv/bin/python -m pytest -q tests/editor tests/conformance
npm --prefix editors/vscode/zlang-hdl test
make editor-static
```

`npm test` and both VSIX packaging commands run the static gate first. `make editor-static`
reruns only that gate using the installed locked editor dependencies. It checks
syntax, undefined names and unused locals/parameters in the Node modules and
the composed, unminified browser runtime. Cross-domain browser references are
resolved together; application names are not globally exempted. The pinned
parser is a development dependency and is not included in either VSIX.

Tests reuse `examples/all_syntax.zhl` and compiler-owned capability metadata.
Real TextMate/Oniguruma tests cover nested generic calls, complete operators,
contextual identifiers, comments, byte escapes and incomplete editor input.
Python tests compile expanded snippets in their documented contexts. The VSIX
audit verifies its exact 13-file inventory, bounded compressed and expanded
sizes, the bundled language-client runtime, dependency notices and license bytes.
CI performs these checks without publishing credentials. The Linux-x64 audit
also requires exactly two stripped ELF tools and rejects Rust/TypeScript source,
Cargo metadata, source maps, and host-specific build paths.

`npm run test:host -- /absolute/package.vsix` is a dev-only extension-host gate.
It downloads the exact stable version recorded in `editor-toolchain.json`,
installs the VSIX into a fresh temporary profile and workspace, and rejects a
different host version. It tests real file recognition, activation-ready
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

The build-only dependency override pins Azure Identity's MSAL Node to 7.1.2
for [GHSA-xcx6-jx8p-x3h5](https://github.com/advisories/GHSA-xcx6-jx8p-x3h5).
The current Identity release still requests an affected major; do not remove
the override until an upstream-compatible fix passes the complete audit and
packaging tests. `qs` is locked to 6.17.0 for
[GHSA-6pq8-jhm2-2vcw](https://github.com/advisories/GHSA-6pq8-jhm2-2vcw).
`npm test` checks dependency loading, credential construction and query
serialization; VSIX host tests qualify local packaging and editor behavior.
These tests do not exercise Marketplace authentication or publication.
`make editor-advisory-audit` checks all locked dependencies, including build
tools. The override and build dependencies are excluded from the VSIX.
