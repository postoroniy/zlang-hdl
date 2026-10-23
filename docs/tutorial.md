# Two complete workflows

These routes use a matching a24 compiler, native runtime and Linux-x64 VSIX.
The universal VSIX supplies editor/LSP support, not the native debugger.
See [installation](installing-toolchain.md) for wheel-only and WSL2 alternatives.

## Install, simulate Counter and open a saved waveform

From the root of a Git checkout:

```sh
make venv
source .venv/bin/activate
zlang examples/counter.zhl --top Counter --check
zlang sim examples/counter.zhl --top Counter --events examples/counter.events.jsonl --trace build/counter.fst
```

Open `build/counter.fst` in VS Code with the ZLang HDL waveform editor.
Add `Counter.clk`, `Counter.rst` and `Counter.y` from the Design Browser.
Select a row before using Change to navigate that signal's transitions.
The checked-in schedule asserts reset for one event, then releases it and
increments twenty times: `y` ends at 20. The bounded clock/cycle alternative
does not invent a reset sequence; use JSONL for explicit reset/input stimulus.
See [native simulation](native-jit-simulation.md) and
[saved/live waveform controls](waveform-workspace.md).

Native trace time is **event-domain ticks**, two ticks per simulator event.
It is not measured hardware nanoseconds and does not imply a physical frequency.
External VCD/FST retains its genuine declared timescale.

## RuleAction: F9, Continue, highlighted source and live signals

1. Open the checkout folder in VS Code; run `make venv` and reload the window
   after installing the matching Linux-x64 extension.
2. Open [`examples/rule_action.zhl`](../examples/rule_action.zhl).
   Put the caret on the executable `count <-` rule line and press **F9**.
   A red dot means a configured breakpoint, not that execution is stopped there.
3. Choose the checked-in RuleAction launch configuration and press **F5**.
   It uses [the saved event schedule](../examples/rule_action.events.jsonl).
4. Press **Continue**. When the breakpoint is reached, VS Code highlights
   the current source statement and shows the paused stack/variables.
5. Select a value in Variables and invoke **Add to Waveform**. Step and observe
   the distinct LIVE marker. Moving waveform cursors does not run the simulator.

The [debugger guide](native-debugging.md) contains the complete `launch.json`,
event schedule, supported types, reset behavior and limitations.

## Where stimulus comes from

JSONL is newline-delimited JSON authored by the user or emitted by a test-vector
generator. Each event sets inputs/resets and advances named clock edges. It is
shared by the CLI, native debugger and native-versus-RTL comparison.
For bounded interactive exploration, a `clock`/`cycles` launch can run without
an event file; while paused, change supported input values. Reset is still explicit.

A Python testbench can drive the native simulation API directly and check values
without writing JSONL. This is a separate Python workflow: Python breakpoints
do not share one stepping engine with `.zhl` breakpoints in the native debugger.
Export JSONL from Python when the same stimulus must be replayed under that debugger.
