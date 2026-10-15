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
