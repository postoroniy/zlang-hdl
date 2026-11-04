<a id="reference-native-simulation"></a>
<a id="reference-simulation-verification"></a>
# Native simulation

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

For ordinary command-line use, choose either a bounded single-clock run or an
explicit event schedule:

```sh
zlang sim examples/counter.zhl --top Counter \
  --clock clk --cycles 100 --json
zlang sim examples/counter.zhl --top Counter \
  --events examples/counter.events.jsonl --trace build/trace.fst
zlang sim examples/add.zhl --top Add --set a=200 --set b=55 \
  --compare-with iverilog --compare-artifacts build/sim-compare --json
```

Use `--set NAME=JSON` for ordinary typed binary inputs. Repeat it for multiple
ports. `--cycles N --clock NAME` is a convenience for one-clock designs whose
reset/input state is already suitable. Use `--events` for resettable designs,
multiple clocks, coincident edges, or changing inputs. For `examples/counter.zhl`
(`Counter`), this schedule resets, releases, then advances four more edges:

```jsonl
{"reset":{"rst":true},"edges":["clk"]}
{"reset":{"rst":false},"edges":["clk"]}
{"edges":["clk"],"repeat":4}
```

`set` changes input values before the event. `reset` uses assertion booleans:
`true` asserts the named reset and `false` releases it, independently of the
declared physical polarity. For an active-low reset, `true` therefore drives
the pin low. `edges` contains every clock edge committed atomically from one
pre-edge snapshot; therefore coincident clocks belong in the same array.
Synchronous reset takes effect on its domain edge, while supported asynchronous
assertion takes effect in the event that changes the reset level.

Each non-empty `--events` JSONL line is one simulation event. An event may add
`"repeat": N` to apply the same updates, resets and edges for `N` consecutive
events; `N` must be a positive integer and the expanded schedule is bounded to
1,000,000 events. The CLI retains one parsed mapping and repeat count per JSONL
record rather than copying its nested payload `N` times. This keeps long idle
or backpressure intervals declarative without requiring a project-specific
event generator.

## Four-state initialization and diagnostics

Ordinary mode uses deterministic binary-zero startup for state without an
initializer. With `--logic-state`, register bits without an initializer start at
`U`, meaning they have never been refreshed; declared initial values remain
known. A write derived from unresolved state
produces `X`; exact controlling values still resolve (`U | 1 == 1`,
`X & 0 == 0`). These are simulation diagnostics, not electrical `Z`, drive
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

## FST/VCD waveforms and differential RTL comparison

`--trace PATH.fst` writes the preferred compact native waveform;
`--trace PATH.vcd` retains VCD interoperability with external and formal tools.
The suffix selects the format. Other suffixes, including `.ghw`, are rejected.
Physical clocks and resets are always present; repeat `--trace-signal NAME` to
keep only selected data/state signals. The timeline uses two event-domain ticks per
simulation event and reflects the declared edge/reset contract. It is an
event visualization, not an analog delay model or physical frequency measurement.
A serialized file timescale does not turn native events into hardware nanoseconds.
Standard FST and VCD both
collapse unresolved provenance to `x`, so ZLang emits hidden
`__zlang_meta/*_u_mask` companions that its query service combines back into
distinct `U` and `X` values.

### Compare native and RTL from one command

The native engine and both RTL comparisons share the `zlang sim` entry point.
ZLang generates Direct-SV, an event-driven testbench and physical input stimulus,
invokes the requested simulator, and compares output samples automatically.
Icarus requires `iverilog` and `vvp` on `PATH`. Verilator requires `verilator`
and its C++ build tools. Use the matching native runtime from the
[toolchain installation](installing-toolchain.md).

Run the following from the checkout root after activating its `.venv`.
Each backend gets its own fresh artifact directory.

```sh
run_dir=$(mktemp -d /tmp/zlang-XXXXXX)

# Native execution
zlang sim examples/counter.zhl --top Counter \
  --events examples/counter.events.jsonl --json

# Native versus Icarus Verilog
zlang sim examples/counter.zhl --top Counter \
  --events examples/counter.events.jsonl \
  --compare-with iverilog --compare-artifacts "$run_dir/iverilog" --json

# Native versus Verilator
zlang sim examples/counter.zhl --top Counter \
  --events examples/counter.events.jsonl \
  --compare-with verilator --compare-artifacts "$run_dir/verilator" --json

printf '%s\n' "$run_dir"
```

The checked-in schedule contains 21 expanded events, including reset, and every
run finishes with `{"y":20}`. With `--json`, stdout contains the output snapshot
for each event. A zero comparison exit code confirms that both executions
completed with matching samples.

Each comparison directory retains `design.sv`, `compare_tb.sv`,
`zlang-compare-events.hex`, `compile.log`, `run.log` and, on success,
`comparison.json`. The JSON record includes product identities, the event count
and native/RTL samples. `--compare-timeout` bounds each external compile/run
command, with a default of 120 seconds. For a native waveform, add
`--trace "$run_dir/native.fst"` to the native command.

Ordinary comparisons use binary simulation. Four-state diagnostic options have
their separate native contract above. Coverage follows the supplied event
schedule. Formal verification has its own proof workflows. For typed file input
and transaction logs, use the
[simulation I/O example](simulation-io.md#cli-and-independent-rtl-comparison).

Comparison checks every physical output after every event. A mismatch, X/Z value,
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
`eval()` settles combinational logic without advancing a clock. Reset assertion
follows the declared synchronous/asynchronous contract; it is not an ordinary
combinational input update. `edge_many()` computes all next-state
values from one pre-edge snapshot and commits atomically. Synchronous resets
act on their domain edge, while supported asynchronous assertion acts
immediately. Values are exact-width and are never silently truncated.

<!-- native-simulation-limits:start -->
The following are implementation limits of this compiler release's native
simulator, not ZLang language semantics. Native simulation accepts at most:

- 32,768 compiled simulation operations;
- a cumulative packed-value work budget of 32,768 machine words;
- 16 MiB of compiled simulation data;
- packed values up to 8,192 bits;
- arithmetic operands up to 512 bits; and
- memory elements up to 8,192 bits.

Generated functional work is also bounded to 8 nested regions, 1,000,000 total iterations, and an estimated runtime-work budget of 8,000,000 operations. Compilation fails with a diagnostic when a limit is exceeded.
<!-- native-simulation-limits:end -->
There is no silent fallback to an RTL simulator. Native simulation requires the
matching Linux x86-64/WSL2 wheel described in the installation chapter; an
unsupported design or missing runtime produces an error.

## Debug without an event file

The Linux-x64 VS Code extension can run a bounded `clock`/`cycles` launch
without JSONL and edit ordinary input ports while paused. It does not generate
a reset sequence; use JSONL for reproducible reset/input schedules or multiple
clocks. The [manual-input debugger workflow](native-debugging.md#debug-without-an-event-file)
owns the launch configuration, F9/Continue/source highlight and live-waveform
steps. `events` and `clock`/`cycles` remain mutually exclusive.

## Python simulation without an event file

The Python API can drive inputs, reset and clock edges directly, without an
event file. See the [complete Python testbench](native-debugging.md#python-simulation-without-an-event-file)
and its expected counts. Python breakpoints run in a separate simulation
instance, not the ZLang source debugger or its live waveform. Export JSONL
from the testbench when the same stimulus must be replayed there.

## Simulation-only typed file I/O

Use [simulation-only source I/O](simulation-io.md) for conditional typed JSONL reads,
pre-edge structured logs and explicit EOF behavior. Activation requires a named
`simulation` block and channel bindings; inactive blocks do not alter hardware.
The runnable `examples/simulation_io.zhl` includes reset, stalls and drain;
VS Code provides **Debug simulation I/O**. Use fresh file outputs or console `-`.
