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
- a bounded `clock` plus `cycles` launch, with ordinary input ports editable
  while paused. This mode does not generate a reset sequence.

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

The clone includes [`.vscode/launch.json`](../.vscode/launch.json) and
[`examples/rule_action.events.jsonl`](../examples/rule_action.events.jsonl)
for [`examples/rule_action.zhl`](../examples/rule_action.zhl). Open the repository
folder in VS Code; there is no event file to generate before pressing F5.
The checked-in schedule is:

```jsonl
{"reset":{"rst":true},"set":{"enable":0},"edges":["clk"]}
{"reset":{"rst":false},"edges":["clk"]}
{"set":{"enable":1},"edges":["clk"],"repeat":2}
{"set":{"enable":0},"edges":["clk"]}
```

The checked-in launch configuration is shown below. In another workspace, add
an equivalent entry to its existing `.vscode/launch.json`:

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
      "events": "${workspaceFolder}/examples/rule_action.events.jsonl",
      "logicState": true,
      "strictUninitialized": false,
      "initialRegisters": {},
      "traceSignals": ["clk", "rst", "enable", "fired", "count"],
      "stopOnEntry": true
    }
  ]
}
```

Open `examples/rule_action.zhl`, **Save All**, select
**Run and Debug → Debug RuleAction**, and press **F5**. The session pauses before
the first event. **F10 / Step Over** advances one complete simulation event,
not one source statement. The five steps produce `count = 0, 0, 1, 2, 2`:
reset, release, two enabled increments, then hold. The declared `= 0` also sets
the initial `count` value to zero; uninitialized state without a reset value is `U`.
Click the gutter to the left of line 10 (`when enable`), or place the caret on
that line (or the `count <- ...` line inside it) and press **F9**, to toggle a
source breakpoint. Press **F5 / Continue**
after the entry stop: the debugger stops after event three with `count = 1`;
Continue again stops after event four with `count = 2`. The breakpoint is solid
red once verified; a hollow marker means that line has no executable site.
On a hit, VS Code highlights the selected source line and shows its execution
arrow (the exact colors follow your theme). The highlight marks the completed
atomic event, not a partially executed statement. It stays while you inspect
values and clears when execution resumes. Several breakpoints in one action
stop once at the first selected line; they do not split the hardware event.
No `debug.allowBreakpointsEverywhere` setting is needed. **Restart** starts
over; **Shift+F5** ends the session.

Use **ZLang HDL: Open Live Waveform** from the Command Palette to inspect the
active session beside the source. Keep **Follow Live** enabled so cursor A and
its value column follow each step. Signals named in `traceSignals` have history
from event zero. While paused, right-click a value in **Variables** or **Watch**
and choose **Add to Waveform**. A newly added signal records from the next
event; restart the session when complete history is required. A saved FST/VCD
tab is separate from this live view and does not follow debugger steps.

Clocks and resets are recorded automatically from event zero, even when omitted
from `traceSignals`. For RuleAction, the live viewer includes `clk` and `rst`;
they are also available under **Variables → Clocks and resets** and in
**ZLang HDL: Add Live Trace Signals**. A saved layout still controls which rows
are displayed. If it predates these controls, or a row was removed, click **+**
beside `clk` or `rst` in the live Design Browser to show it again.

Clock pulses follow the declared rising or falling edge. Reset levels follow
the declared active-high or active-low polarity; JSONL `true` means *asserted*,
not necessarily a physical `1`. Within event `n`, reset changes appear at tick
`2n−1`, the clock edge and resulting values at `2n`, and the clock returns to
idle at `2n+1`. Coincident clocks share the same edge tick. This is an event
visualization, not a physical delay or frequency model. The clock/reset scope
is read-only: change reset through the event file, not by editing its displayed
value. After updating compiler and VSIX together, reload the VS Code window and
start a new debug session.

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
  --events examples/rule_action.events.jsonl --logic-state --json
```

For generated vectors, make the generator write the same JSONL schema and
check the generated file into the project only when it is stable test data.
Transient or large generated schedules belong under ignored `build/` output.
The debugger never runs an arbitrary vector-generator command as part of
launch.

## Debug without an event file

For an interactive single-clock experiment, add this entry to the
`configurations` array in `.vscode/launch.json`. It needs no JSONL file:

```json
{
  "type": "zlang-hdl",
  "request": "launch",
  "name": "RuleAction - manual inputs",
  "program": "${workspaceFolder}/examples/rule_action.zhl",
  "top": "RuleAction",
  "clock": "clk",
  "cycles": 20,
  "logicState": true,
  "stopOnEntry": true,
  "traceSignals": ["enable", "fired", "count"]
}
```

1. Save `examples/rule_action.zhl`, select **RuleAction - manual inputs**, and
   press **F5**. The debugger stops before the first clock event.
2. Expand **Variables > Ports**, right-click `enable`, choose **Set Value**,
   and enter `1`.
3. Press **F10** twice. **Registers > count** shows `1`, then `2`.
4. Change `enable` to `0` and step again. `count` holds at `2`.
5. Set `enable` back to `1`, place an **F9** breakpoint on `when enable` or
   its `count <- ...` action, then press **F5 / Continue**. The next enabled
   event stops with `count = 3` and highlights the selected source line.
6. Open **ZLang HDL: Open Live Waveform** and keep **Follow Live** on to
   observe the same events. Editing an input does not advance the clock; its
   next recorded sample appears when another event executes.

`count` starts at its declared `= 0`; this example does not assert reset.
Clocks, resets, registers and memories are read-only in Variables. Use JSONL
for reset assertion/release, coincident clocks, automated input changes or a
reproducible protocol exchange. `events` and `clock`/`cycles` are mutually
exclusive. The session ends after 20 clock events; increase `cycles` and
relaunch for a longer experiment. Restart restores the declared initial state,
not a recording of manual input edits.

## Python simulation without an event file

For programmatic stimulus, a Python testbench can drive the native simulator
directly. Save this as `tb_rule_action.py` in the repository root:

```python
from zlang import sim

with sim.load("examples/rule_action.zhl", top="RuleAction", logic_state=True) as dut:
    dut.set("enable", 0)
    dut.reset("rst", asserted=True)
    dut.edge("clk")
    assert dut.get("count") == 0

    dut.reset("rst", asserted=False)
    dut.edge("clk")
    for enable, expected in [(1, 1), (1, 2), (0, 2)]:
        dut.set("enable", enable)
        dut.edge("clk")
        count = dut.get("count")
        print(f"enable={enable}, count={count}")
        assert count == expected
```

Run it from the repository root with `.venv/bin/python tb_rule_action.py`.
The final three counts are `1`, `2`, `2`. In VS Code, use the Python Debugger
with this checkout's `.venv/bin/python` to put breakpoints in the Python file.
F10 there steps Python statements; `dut.edge("clk")` advances one hardware
event.

This Python API creates a separate simulation instance. It does not connect
to a running ZLang debug session: `.zhl` breakpoints and **Open Live Waveform**
do not follow it. For those features, use a `zlang-hdl` launch with manual
inputs as above, or replay a Python-generated JSONL schedule. The ZLang
debugger does not execute the Python testbench itself.

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

While paused, expand a port, register, or memory in **Variables**. Right-click
the value or one of its fields and choose **Add to Waveform**. The same command
is available on a signal in **Watch**. Watch accepts exact published signal
paths, such as `packet.mode` or `buffer[3].value`, not arbitrary expressions.
The live panel opens automatically. **ZLang HDL: Add Live Trace Signals** offers
a multi-select list of whole ports/registers/interfaces.

- Adding a struct, tuple or vector adds its packed row and typed child rows.
  Select a field/index instead when only that part is needed. Large arrays are
  paged in Variables; whole selections exceeding 4096 trace rows are rejected.
- Enum rows show their declared member and exact code, including sparse codes.
  Invalid codes are labelled invalid; U/X values are not decoded as members.
- Signed integers retain signedness. Fixed-point rows show the exact scaled
  value as `raw/2^fraction` together with its raw bits. Select Hex/Binary to
  inspect the packed representation. Struct/tuple/vector fields use the same
  compiler-owned layout as execution, with no host-word-width truncation.
- Tagged unions show the tag and per-variant fields. Inactive fields say
  **inactive variant**; an unknown tag says **unknown variant**. These labels
  are presentation states, not extra logic values or guessed payloads.
- Interfaces are groups, not packed structs. Their payload/control fields keep
  their individual directions and clock domains. Ready/valid includes payload,
  valid and ready; aggregate bus interfaces retain their channel groups.
- Expand a memory and select individual cells/fields. The entire memory is not
  implicitly recorded. Registers and memory remain read-only in the debugger.

**Type** is the default format for these added live rows. Adding a signal during
a run starts recording at the next atomic event. Earlier history is **not
recorded**, rather than U or X; restart for complete history. A whole selection
is validated before its trace configuration changes. Multiple field rows share
one recorded backing value.

Typed metadata is a debugger overlay, independent of the production simulation
plan and RTL. The compiler and native adapter must support the same overlay
version; update the matching VSIX when updating the compiler. Saved VCD/FST
exports currently contain recorded raw backing signals. They do not preserve
the live catalog's nominal struct/union types; external enum metadata is used
only when actually present in the file. Session sidecars save presentation,
not type guesses or waveform samples.

The [waveform workspace guide](waveform-workspace.md) covers hierarchy browsing,
groups, multiple viewports, measurements, navigation, and portable sessions.

Live and saved views share these controls, including the 16-color palette, row
height, duplicate rows, Cut/Copy/Paste, search and measurements. In the live
toolbar, **Add signals** opens the trace selector and **Refresh** only refreshes
recorded data; it never advances or restarts simulation. **Save Session** saves
a layout template for another live session; **Load Session** restores it using
exact recorded signal names and widths. The workspace also recovers the layout
when the panel is reopened. **Export Trace** saves FST/VCD data for inspection
after the debug session ends. Late-added history is still not backfilled.

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

Native waveform time is event-index visualization: two ticks represent one
simulation event. Native traces are labelled `zlang_event_ticks` and have no
physical frequency measurement. External VCD/FST retains its declared timescale.

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
- `zlang-waveform-query-v3` identifies bounded readonly FST/VCD hierarchy,
  value, search and digital-measurement queries.

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

Time is event-index visualization: two event-domain ticks per simulation event.
It is not an analog timing model or physical frequency measurement. Trace
storage is delta encoded, chunked, capped
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
