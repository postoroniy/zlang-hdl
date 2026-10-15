# Debugging native simulation in VS Code

> a24 status: compiler debug-bundle support and the Linux-x64 native adapter /
> waveform implementation are a bounded preview. The universal VSIX remains
> editor/LSP-only. The enhanced Linux-x64 VSIX contains two reviewed native
> executables and no Rust implementation source.

This guide describes the user workflow. The debugger runs the same native
simulation plan as `zlang sim`; it does not synthesize test vectors or infer a
clock/reset sequence. Stimulus comes from either:

- a user-authored JSONL event file, normally kept in a project's
  `simulation/` directory;
- a JSONL file produced by the project's existing test-vector generator; or
- a simple bounded `clock` plus `cycles` launch for a design that needs no
  explicit reset or per-event input changes.

The same JSONL file can be used by the command-line simulator, the VS Code
debugger, and native-versus-RTL comparison. Keeping stimulus outside the
debugger makes a failing run reproducible without VS Code.

## Prerequisites

Use the Linux-x64 VSIX; the universal VSIX contains editor and LSP support but
not the native debug adapter. The compiler, native runtime, language server,
VSIX, and bundled debug tools must come from the same ZLang HDL release.

From a source checkout:

```sh
make venv
source .venv/bin/activate
.venv/bin/zlang-lsp --version
```

Install the matching `zlang-hdl-0.1.0-linux-x64.vsix`, set
`zlang.lsp.path` to `${workspaceFolder}/.venv/bin/zlang-lsp`, and reload the
VS Code window. Save every relevant `.zhl` file before starting a session. The
compiler rejects a debug launch when an open source buffer and the saved source
snapshot differ.

## Quick start

The following schedule drives [`examples/rule_action.zhl`](../examples/rule_action.zhl).
Create `build/rule-action-events.jsonl`:

```jsonl
{"reset":{"rst":true},"set":{"enable":0},"edges":["clk"]}
{"reset":{"rst":false},"edges":["clk"]}
{"set":{"enable":1},"edges":["clk"],"repeat":2}
{"set":{"enable":0},"edges":["clk"]}
```

Create or extend `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "type": "zlang-hdl",
      "request": "launch",
      "name": "Debug RuleAction",
      "program": "${workspaceFolder}/examples/rule_action.zhl",
      "top": "RuleAction",
      "events": "${workspaceFolder}/build/rule-action-events.jsonl",
      "logicState": true,
      "strictUninitialized": false,
      "initialRegisters": {},
      "traceSignals": ["fired", "count"],
      "stopOnEntry": true
    }
  ]
}
```

Open `examples/rule_action.zhl`, place a breakpoint on `when enable`, select
**Run and Debug → Debug RuleAction**, and press **F5**. A verified breakpoint
stops after the atomic rule event; Variables shows the current typed values and
the previous/current values associated with that event. **Step Over** advances
one complete simulation event, not one source statement.

Use **ZLang HDL: Open Live Waveform** from the Command Palette to inspect the
active session. Signals named in `traceSignals` have history from event zero.
Signals added while the session is running start at the event where they were
added; restart the session when complete history is required.

## JSONL event files

Each non-empty line is one atomic event object:

- `set` assigns ordinary typed input values;
- `set_logic` assigns packed `0`/`1`/`U`/`X` values;
- `reset` asserts or releases named physical reset inputs;
- `edges` lists clock edges that occur atomically from one pre-edge snapshot;
- `repeat` repeats that event without materializing duplicate event objects.

Inputs and reset levels remain unchanged until a later event replaces them.
`repeat` must be positive, and one run is bounded to one million expanded
events. Errors retain the JSONL source line so a malformed or invalid vector
can be fixed at its origin.

Before opening VS Code, validate the exact stimulus from the terminal:

```sh
.venv/bin/zlang sim examples/rule_action.zhl --top RuleAction \
  --events build/rule-action-events.jsonl --json
```

For generated vectors, make the generator write the same JSONL schema and
check the generated file into the project only when it is stable test data.
Transient or large generated schedules belong under ignored `build/` output.
The debugger never runs an arbitrary vector-generator command as part of
launch.

For a simple single-clock run, replace `events` with both `clock` and a bounded
`cycles` count:

```json
{
  "type": "zlang-hdl",
  "request": "launch",
  "name": "Run 100 clock events",
  "program": "${workspaceFolder}/examples/counter.zhl",
  "top": "Counter",
  "clock": "clk",
  "cycles": 100,
  "traceSignals": ["y"]
}
```

`events` and `clock`/`cycles` are mutually exclusive. Prefer JSONL when reset,
multiple clocks, input changes, coincident edges, or a reproducible protocol
exchange matters.

## Breakpoints, stepping, and values

- Source breakpoints are valid only on compiler-published `when` rules, FSM
  transitions, and verification clauses. VS Code leaves other breakpoints
  unverified instead of pretending they can execute.
- One DAP thread represents the simulation. Stack frames identify the active
  hierarchy/source event; they are not software call frames.
- **Continue** runs until a verified source/data breakpoint, exception, or the
  end of the bounded event schedule. **Pause** takes effect after the current
  atomic event.
- Data breakpoints stop when the selected signal, register, or memory cell
  changes. Registers and memories are read-only. Input ports may be changed
  while paused.
- Assertion, protocol, strict-U/X, and runtime failures are debugger
  exceptions with compiler-owned source attribution.
- `initialRegisters` accepts the same resolved register names and values as
  the simulation initial-state interface. Use it to reproduce a deliberate
  initial condition, not to hide a missing reset.

## Live and saved waveforms

The live panel follows the current debug session. The readonly waveform editor
opens saved `.fst` and `.vcd` files independently of a debug session. FST is
preferred for compact native traces; VCD remains useful for interchange with
external RTL and formal tools.

- Click a row to select and highlight a signal.
- **Change**, its direction buttons, and Left/Right move among changes of the
  selected signal only.
- Mouse-wheel zooms around the pointer; **Shift+wheel** pans in time.
- Hold the middle mouse button and drag left/right to pan in time or up/down to
  scroll signals.
- Click places cursor A; **Shift+click** places marker B.
- Double-click restores the complete trace.

Waveform time is event-index visualization: two 1 ns ticks represent one
simulation event. It is not an analog timing claim.

## Project launches

For a project root, add `project` and optionally `profile`:

```json
{
  "type": "zlang-hdl",
  "request": "launch",
  "name": "Debug project top",
  "program": "${workspaceFolder}/src/top.zhl",
  "top": "Top",
  "project": "${workspaceFolder}/zlang.toml",
  "profile": "portable",
  "events": "${workspaceFolder}/simulation/events.jsonl",
  "traceSignals": ["input.valid", "input.ready", "output.valid"]
}
```

Project compilation validates the existing `zlang.lock` read-only. Debugging
does not fetch dependencies or update the lock.

## Troubleshooting

- **No debug configuration:** confirm that the Linux-x64 VSIX is installed,
  not only the universal editor/LSP package.
- **Language server unavailable:** run `.venv/bin/zlang-lsp --version` and
  inspect **View → Output → ZLang HDL** for the exact executable path.
- **Save All before debugging:** save the relevant `.zhl` files and relaunch;
  the adapter will not combine a stale compiled bundle with dirty source.
- **Breakpoint is unverified:** move it to a rule, FSM transition, or
  verification clause published by the compiler.
- **No waveform history:** list the signal in `traceSignals`, or add it live and
  restart when history from event zero is needed.
- **Runtime/adapter version mismatch:** install compiler, native wheel, and
  Linux-x64 VSIX from the same release.

## Architecture and contracts

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
the stripped `zlang-debug-adapter` and `zlang-waveform-indexer` ELF
executables. It ships no Rust/TypeScript source, Cargo metadata, debug symbols,
source maps, runtime downloader, or private build paths. The two binaries are
admitted by exact path, size and SHA-256 in `release/status.json`; public CI
audits those bytes before placing them in the VSIX.

Install the platform package downloaded from the matching GitHub release:

```sh
code --install-extension zlang-hdl-0.1.0-linux-x64.vsix --force
```

The public source checkout can reproduce the package envelope from the same
reviewed executable bytes with `make editor-native-host-test`. Rebuilding the
native Rust implementation is deliberately a release-host operation and is
not presented as a public-source command.
