<a id="reference-simulation-verification"></a>
## Simulation, waveforms, and verification

### Native simulation

Native simulation runs a checked design without first generating
SystemVerilog or starting an external HDL simulator. Use the Python API when a
test needs direct input, state, and clock control:

```python
from zlang import sim

program = sim.compile("examples/counter.zhl", top="Counter")
instance = program.create()
instance.tick("clk")
print(instance.get("y"))
instance.close()

```

### Inputs, clocks, resets, and event schedules

For ordinary command-line use, choose either a bounded single-clock run or an
explicit event schedule:

```sh
zlang sim examples/counter.zhl --top Counter \
  --clock clk --cycles 100 --json
zlang sim examples/multi_clock_stateful.zhl --top MultiClockStateful \
  --events events.jsonl --trace build/trace.fst
zlang sim examples/add.zhl --top Add --set a=200 --set b=55 \
  --compare-with iverilog --compare-artifacts build/sim-compare --json
```

Use `--set NAME=JSON` for ordinary typed binary inputs. Repeat it for multiple
ports. `--cycles N --clock NAME` is a convenience for one-clock designs whose
reset/input state is already suitable. Use `--events` for resettable designs,
multiple clocks, coincident edges, or changing inputs. For example:

```jsonl
{"reset":{"rst":true},"edges":["clk"]}
{"reset":{"rst":false},"set":{"load":1,"value":37},"edges":["clk"]}
{"set":{"load":0},"edges":["clk"],"repeat":4}
```

`set` changes input values before the event. `reset` sets the named physical
reset signal level. `edges` contains every clock edge committed atomically from
one pre-edge snapshot; therefore coincident clocks belong in the same array.
The reset spelling is the pin level, so an active-low reset is asserted with
`false`. Synchronous reset takes effect on its domain edge, while supported
asynchronous assertion takes effect in the event that changes the reset level.

Each non-empty `--events` JSONL line is one simulation event. An event may add
`"repeat": N` to apply the same updates, resets and edges for `N` consecutive
events; `N` must be a positive integer and the expanded schedule is bounded to
1,000,000 events. The CLI retains one parsed mapping and repeat count per JSONL
record rather than copying its nested payload `N` times. This keeps long idle
or backpressure intervals declarative without requiring a project-specific
event generator.

### U/X diagnostics

Ordinary mode uses deterministic binary-zero startup for state without an
initializer. `--logic-state` instead initializes every unreset register bit to
`U`, meaning it has never been refreshed. A write derived from unresolved state
produces `X`; exact controlling values still resolve (`U | 1 == 1`,
`X & 0 == 0`). This is hardware-debug information, not electrical `Z`, drive
strength, or the Verilog event-region model.

Packed four-state spellings are MSB-first strings containing `0`, `1`, `u`, or
`x`. Use `--set-logic NAME=01ux` for an input and
`--initial-reg NAME=01ux` to override one named register path
before the first event. The width must match exactly. `--strict-uninitialized`
implies logic-state execution and fails when a typed observation requires an
unresolved `U` or `X`; it does not alter compilation or Direct-SV.

Both modes execute the selected implementation. U/X tags describe diagnostic
uncertainty, not additional physical hardware values. Legal exact binary
optimizations can change U/X propagation, so the waveform is not a record of
every operand in the original source spelling. Strict mode checks unresolved
typed observations and stateful controls; resolved cancellation or controlling
values need not fail. Traces and `get_logic()` retain tags for inspection.

### Waveforms and differential RTL comparison

`--trace PATH.fst` writes the preferred compact native waveform;
`--trace PATH.vcd` retains VCD interoperability with external and formal tools.
The suffix selects the format. Other suffixes, including `.ghw`, are rejected.
Physical clocks and resets are always present; repeat `--trace-signal NAME` to
keep only selected data/state signals. The timeline uses two 1 ns ticks per
simulation event and reflects the declared edge/reset contract. It is an
event visualization, not an analog delay model. Standard FST and VCD both
collapse unresolved provenance to `x`, so ZLang emits hidden
`__zlang_meta/*_u_mask` companions that its query service combines back into
distinct `U` and `X` values.

`--compare-with iverilog` and `--compare-with verilator` emit the normal direct
SystemVerilog artifact, execute it in the selected external simulator, and
compare every physical output after every event. A mismatch, X/Z value,
missing sample, tool failure, or timeout fails closed. Comparison artifacts
are retained only when an empty `--compare-artifacts` directory is supplied.
Sequential RTL must receive the same explicit reset events needed by physical
hardware; use `--events` when comparing a resettable design.

Exit status is nonzero for compilation/plan errors, native runtime failures,
strict-U/X violations, verification/protocol failures, comparison mismatch,
external tool failure, or timeout. Machine consumers should use `--json` and
still treat the process status as authoritative. `--compare-artifacts` must
name a new empty directory; without it, temporary comparison material is
discarded.

One instance is not thread-safe; independent instances may run concurrently.
`eval()` changes no architectural state. `edge_many()` computes all next-state
values from one pre-edge snapshot and commits atomically. Synchronous resets
act on their domain edge, while supported asynchronous assertion acts
immediately. Values are exact-width and are never silently truncated.

There is no silent fallback to an RTL simulator. Native simulation requires the
matching Linux x86-64/WSL2 wheel described in the installation chapter; an
unsupported design or missing runtime produces an error.

### Debug native simulation in VS Code

The Linux-x64 ZLang HDL extension debugs the same native simulation plan used
by `zlang sim`. It does not generate stimulus. Give the debugger either the
same JSONL event schedule used by the command line or a bounded single-clock
`clock` plus `cycles` launch.

#### Debug without an event file

The declared `count = 0` supplies the initial state; this experiment does not
assert reset. Clocks, resets, registers and memories are read-only in
Variables. Use JSONL for explicit reset sequences, multiple clocks or
repeatable input changes. `events` and `clock`/`cycles` are mutually exclusive.
The session ends after 20 clock events. Restart restores initial state rather
than replaying manual edits.

For an interactive single-clock experiment, use the launch entry below:

1. Save `examples/rule_action.zhl`, select **RuleAction - manual inputs**, and
   press **F5**. The session stops before the first clock event.
2. Under **Variables > Ports**, right-click `enable`, choose **Set Value**,
   and enter `1`. Two **F10** steps produce `count = 1`, then `2` under
   **Registers**. Each step completes one hardware event, not one source line.
3. Set `enable` to `0` and step again: `count` holds at `2`.
4. Set `enable` back to `1`, use **F9** on `when enable` or its `count <- ...`
   action, then **F5 / Continue**. The next enabled event stops with `count = 3`
   and highlights the selected source line.
5. Open **ZLang HDL: Open Live Waveform** and keep **Follow Live** on to
   watch the same events. Input edits alone do not advance the clock; the
   waveform samples them when another event executes.

Add this entry to the `configurations` array in `.vscode/launch.json`, without
an `events` field:

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

#### Debug with a reproducible event schedule

Stable project vectors normally belong in `simulation/`. Temporary or generated
experiments belong under ignored `build/` output. A project-specific generator
may produce the JSONL file, but the debugger does not run an arbitrary generator
as part of launch. This keeps a failing vector replayable from the terminal,
VS Code, and native-versus-RTL comparison.

For example, save this schedule as `build/rule-action-events.jsonl`:

```jsonl
{"reset":{"rst":true},"set":{"enable":0},"edges":["clk"]}
{"reset":{"rst":false},"edges":["clk"]}
{"set":{"enable":1},"edges":["clk"],"repeat":2}
{"set":{"enable":0},"edges":["clk"]}
```

Then add a `.vscode/launch.json` configuration:

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

Save the relevant `.zhl` files before launch. Source breakpoints are verified
only on executable rules, FSM transitions, and verification clauses. One step
executes one complete atomic simulation/reset/clock event. Continue runs until
a verified source or data breakpoint, an exception, or the end of the bounded
schedule. Registers and memories are read-only while paused; input ports may be
changed. Assertion, protocol, strict-U/X, and runtime failures appear as debugger
exceptions with source attribution.

Use **ZLang HDL: Open Live Waveform** for the active session. Signals listed in
`traceSignals` have history from event zero; a signal added during execution is
recorded from that point forward. Restart when complete history is needed. The
readonly waveform editor also opens saved FST and VCD files independently of a
debug session.

In the waveform view, click a row to select it. Change navigation and the
Left/Right keys then follow transitions of that signal only. Mouse-wheel zooms
around the pointer, Shift+wheel pans in time, and middle-button drag pans in
time or scrolls the signal list. Waveform time retains the event-index meaning
described above.

#### Python simulation without an event file

A Python testbench can supply inputs and clock/reset operations directly:

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

Save it as `tb_rule_action.py` in the repository root and run
`.venv/bin/python tb_rule_action.py` there. The final three counts are `1`, `2`,
`2`. For Python source breakpoints in VS Code, select this checkout's
`.venv/bin/python` in the Python Debugger. F10 steps Python statements;
`dut.edge("clk")` advances one hardware event.

This API creates a separate native instance, not a connection to the ZLang
debug session. ZLang source breakpoints and **Open Live Waveform** do not
follow a Python-driven instance. To use them, choose the manual-input launch
above or replay a Python-generated JSONL schedule. The debugger does not run
the Python testbench itself.

<a id="reference-getting-started"></a>

### Source-level verification

Place goals in the source next to the hardware they describe:

```zlang
module CheckedCounter {
    clock clk reset rst
    in enable : bit
    out value : u8

    reg count : u8 = 0
    step: when enable { count <- truncate<8>(count + 1) }
    value = count

    assert never_wraps_early @ clk { (count < 255) | !enable }
    cover reaches_ten @ clk { count == 10 }
}
```

Run all applicable goals for a selected top:

```sh
zlang design.zhl --top CheckedCounter --verify \
  --formal-jobs 4 \
  --verification-report build/verification.txt \
  --verification-work-dir build/verification-work
```

For a reproducible two-step flow, first publish a verification bundle and then
run it:

```sh
zlang design.zhl --top CheckedCounter \
  --verification-bundle build/verify
zlang verify build/verify --mode bmc --depth 20 \
  --work-dir build/verify-work
```

The work directory contains solver logs and any counterexample or cover VCD.
Keep it outside the bundle so reruns do not modify the input being checked.

### Evidence terminology

Result terms have precise meanings:

- `bounded_pass`: no counterexample was found through the stated depth; this is
  not an unbounded proof;
- `proven`: the selected proof flow completed an unbounded proof;
- `witnessed`: a cover goal was reached and has a witness;
- `bounded_unreached`: a cover was not reached within the selected bound;
- `unknown`, `unavailable`, or `skipped`: no proof claim was established.

Each clocked goal names its clock with `@ clock_name`. Reset behavior follows
the module's declared polarity, assertion mode, and release contract. Goals in
independent supported domains run separately; this does not create a
cross-domain temporal property. Use explicit assumptions only for real
environment constraints, and never use an assumption to hide an invalid design.

Formal execution requires Yosys, SymbiYosys, `yosys-smtbmc`, and a supported
solver such as Z3. The installation chapter shows how to check those tools.
Examples are available in
[`examples/verification`](../../examples/verification/README.md).
