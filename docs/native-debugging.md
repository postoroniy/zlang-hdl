# Native simulation debugging and waveforms

> a24 status: compiler debug-bundle support and the Linux-x64 native adapter /
> waveform implementation are a bounded preview. The universal VSIX remains
> editor/LSP-only; an enhanced Linux-x64 VSIX must pass its separate binary
> reproducibility and packaging gates before it may be distributed.

ZLang HDL keeps one language implementation. The Python compiler and
`zlang-lsp` parse, type-check, select implementations, and publish an immutable
debug bundle. Rust executes the ordinary `SimulationPlan`, stores four-state
values, serves the Debug Adapter Protocol (DAP), and indexes waveforms. The
adapter does not parse ZLang source or reconstruct compiler semantics.

```text
saved .zhl + project snapshot
    -> zlang-lsp: zlang/prepareDebugBundle
    -> ordinary SimulationPlan + debug overlay
    -> zlang-debug-adapter (DAP over stdio)
    -> simulation-core + waveform-core
```

The contracts are versioned independently:

- `zlang-native-debug-bundle-v1` describes the compiler product;
- `zlang-debug-overlay-v1` identifies executable source sites separately from
  the production plan;
- `zlang-debug-adapter-v1` identifies the DAP executable contract;
- `zlang-waveform-trace-v1` identifies live range-query results.
- `zlang-waveform-query-v2` identifies bounded readonly FST/VCD file queries.

Presentation paths do not participate in stable compiler identities. Debug
sites and probes do not alter semantic identity, selected implementation
identity, or the ordinary simulation-plan identity. Relevant `.zhl` buffers
must be saved before launch so source digests and breakpoints refer to the same
snapshot.

## Initial supported surface

The Linux-x64 VSIX contributes debug type `zlang-hdl`. One thread represents
one simulation. A step completes one atomic evaluation, reset, or clock event;
there is no fictional software call stack. Source breakpoints are accepted only
at compiler-published rule, FSM-transition, or verification sites. Data
breakpoints observe signal changes. Registers and memories are read-only;
input ports may be changed while paused. Runtime, assertion, protocol, and
strict-U/X failures stop as DAP exceptions.

An event file is mutually exclusive with a bounded `clock` plus `cycles`
launch. Reverse execution, attach, unbounded run, arbitrary state mutation, and
expression-level stepping are not supported.

Restart reuses the frozen bundle, resets execution and waveform history, and
reapplies the declared initial-register overrides and trace selection.

## Waveforms

The live panel reads bounded delta ranges from the active debug session. The
readonly custom editor opens both `.fst` and `.vcd` through one native query
service. FST is preferred for new native traces; VCD remains a permanent
interchange and formal-tool path. Signals are loaded only when displayed or
searched. The viewer provides hierarchy/filtering, drag ordering, nested named
groups, one to four independent viewports, cursors, transition/value search,
binary/hex/signed display, and pointer-centered zoom/pan. Layout persists per
waveform URI. Standard FST/VCD collapse unresolved provenance to `x`; ZLang's
hidden U-mask companion is combined by the query service so the viewer renders
`U` and `X` distinctly.

Optional Waveform Control Protocol v0 support is disabled by default. In a
trusted workspace, set `zlang.waveform.wcp.enabled` to start a bounded listener
on `127.0.0.1`; `zlang.waveform.wcp.port` selects a fixed port or `0` requests
an ephemeral one. The output channel reports the chosen address. The listener
does not bind non-loopback interfaces and accepts only the documented v0
greeting and command set.

Time is event-index visualization: two 1 ns ticks per simulation event. It
is not an analog timing model. Trace storage is delta encoded, chunked, capped
at one million events, and may spill beneath VS Code `globalStorageUri`.

## Packaging boundary

The universal VSIX remains editor/LSP-only. The enhanced Linux-x64 VSIX adds
stripped `zlang-debug-adapter` and `zlang-waveform-indexer` ELF executables. It ships
no Rust/TypeScript source, Cargo metadata, debug symbols, source maps, runtime
downloader, or private build paths. The native build uses deterministic path
remapping and the package audit rejects host-path leakage.

From a private source checkout:

```sh
make native-editor-build
npm --prefix editors/vscode/zlang-hdl ci --ignore-scripts
npm --prefix editors/vscode/zlang-hdl run package:linux-x64 -- \
  "$PWD/build/zlang-hdl-linux-x64.vsix"
.venv/bin/python tests/editor/test_vscode_package.py \
  "$PWD/build/zlang-hdl-linux-x64.vsix"
```

These commands prepare local evidence only; they do not publish an extension.
