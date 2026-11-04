# Simulation-only source I/O

A `simulation` block describes bounded testbench I/O around an ordinary clocked
design. It is checked by the compiler but adds no ports, state, latency or file
operations to the synthesized DUT. Activation is explicit; merely compiling the
source opens no channels. Packet-arbiter tops retain a specialized endpoint-only backend and reject I/O
activation explicitly. Only a named block on the selected top is activated;
blocks in instantiated children do not run implicitly.

## Source contract

```zlang
module Samples {
    clock clk reset rst
    in sample : s16
    in enable : bit
    out reg previous : s16 = 0
    when enable { previous <- sample }

    simulation vectors @clk {
        when ~rst & enable {
            read stimulus { value -> sample } else { finish }
            write results { value=sample old_value=previous }
        }
    }
}
```

`simulation NAME @ CLOCK` uses an existing clock domain. Its contextual commands
are `when`, `read`, `write`, `drive` and `finish`; these words remain usable as
ordinary identifiers elsewhere. Expressions use normal ZLang typing, widths,
signedness, conversions and nominal types. They cannot introduce a pipeline,
delay or implementation choice. No general loops, arbitrary host calls or
runtime file paths are part of this construct.

One event selecting the block's active clock has these phases:

1. Evaluate read/drive guards and drive operands from the existing native/RTL
   pre-edge snapshot. Each active `read` selects the next record; a false guard
   consumes nothing. Stage input updates without advancing architectural state.
2. Apply the staged inputs together and settle combinational logic. Evaluate
   write guards and write expressions from this new **pre-edge** snapshot.
3. Execute the ordinary atomic clock edge. Commit the selected input cursor and
   staged log records only after the edge succeeds.

Thus `old_value` in the example is the previous register value, not the value
just committed by that edge. A guard containing both reads and writes must not
feed back through a block-owned input. In particular, a combinational output
alias does not conceal such feedback. Write-only guards are evaluated after
inputs settle. All drive operands read the same old input snapshot; source
order does not create procedural read-after-write hardware semantics.

Request/response admission can couple both channels; its read/drive dependency
check is conservative and may reject guards involving block-owned interface
inputs. Use an independent environment control for those file reads.

A target must be an environment-owned top input, its struct field, or an
existing protocol input field. Direction follows the compiler's protocol
ownership: an input ready/valid endpoint owns payload/valid; an output endpoint
owns ready. Clock/reset, registers, DUT-owned outputs and immutable wire aliases
are not drive targets. The block owns its target containers: Python/event/manual
debug edits to those inputs are rejected rather than racing the file driver.

V1 has one input channel, with a fixed ordered schema. Multiple conditional
read sites may share that channel and schema, but at most **one read attempt**
may execute per event. Overlapping unconditional drivers are rejected. Read
success and its EOF branch are exclusive; mutually exclusive sites may target
the same fields. Each output channel also has a fixed ordered schema.

## Records, EOF and failures

The input is a UTF-8 JSONL regular file, frozen and hashed at activation. Blank
lines are ignored; duplicate, missing and extra keys are errors. Names in the
record are the aliases before `->`, not inferred port names. For the example:

```jsonl
{"value":7}
{"value":-11}
```

Values use the Python simulation API's exact typed/raw representation. Fixed
point uses its raw integer, not an approximate JSON decimal. For a type with
fraction 14, raw 16384 means 1.0. An explicit packed form is available for every
payload: `{"bits":"0100000000000000"}`. It requires exactly the declared width;
known encodings are checked against the type. In four-state native simulation,
packed `u` and `x` tags retain their diagnostic meanings. No short-bit broadcast
or silent truncation is performed.

Every `read` requires an `else { ... }` EOF branch. EOF remains selected on later
attempts; reset does not rewind the file. An EOF branch may drive inputs, log,
conditionally act or finish, but cannot read again. `finish` cancels the current
prospective edge and its staged effects and stops the bounded run. Commands
following it are not evaluated. For ready/valid streams an alternative is
`else { drive input.valid=0 }`, letting already accepted data drain.

Output records contain raw bits plus event/epoch coordinates, for example:

```json
{"epoch":0,"event":0,"values":{"old_value":{"bits":"0000000000000000"},"value":{"bits":"0000000000000111"}}}
```

Tuple and tagged-union records can use the exact packed form; JSON does not
construct Python tuple/tagged-union DTOs implicitly. Pure local observations are
compiled from their typed definitions, not resolved by guessing physical RTL nets.

A file output also gets a `.io.json` sidecar with field types, plan/recipe and
source identities, counts and completion/failure status. Native file logs also
record their final digest. Files are created exclusively: an existing log or
sidecar is not truncated. Input, output and sidecar paths must be distinct.
`-` selects structured console output; stdin is not an input channel.

A malformed record, unresolved guard, conflicting external drive, byte limit or
failed hardware check stops the run. It commits no new cursor/log record for
that failed edge. The existing native runtime owns hardware rollback. Completed
logs remain available; an I/O failure during physical file writing can leave a
partial last record and is reported as failure, never resumed as success. Host
I/O is not a transactional filesystem and does not undo external files.

The existing execution ceilings apply: 1,000,000 expanded events, 4,096 commands,
8 nested control regions, and 16 MiB input/prepared-data/combined-output byte
ceilings. Existing native node/width/work ceilings are unchanged. A simulation
block does not disable ordinary assertions, protocol checks or strict U/X
checks. Reset observations in these blocks mean logical reset assertion,
independent of the domain's electrical polarity.

## CLI and independent RTL comparison

The runnable example accepts fixed-point ready/valid samples and exercises reset,
stalls and drain:

```bash
zlang sim examples/simulation_io.zhl --top SimulationIoExample \
  --simulation vectors \
  --sim-input stimulus=examples/simulation_io.vectors.jsonl \
  --sim-output results=build/samples.jsonl \
  --events examples/simulation_io.events.jsonl
```

Create `build` first and choose a fresh output filename for every new run.
The example logs three raw samples (16384, -8192, 16383), at events 3, 4 and 8.
The same `zlang sim` entry point can compile and compare native execution with
Icarus automatically. This complete command keeps its input and event schedule,
and chooses fresh log and RTL artifact paths.

```bash
run_dir=$(mktemp -d /tmp/zlang-XXXXXX)
zlang sim examples/simulation_io.zhl --top SimulationIoExample \
  --simulation vectors \
  --sim-input stimulus=examples/simulation_io.vectors.jsonl \
  --sim-output results="$run_dir/samples.jsonl" \
  --events examples/simulation_io.events.jsonl \
  --compare-with iverilog --compare-artifacts "$run_dir/iverilog"
printf '%s\n' "$run_dir"
```

Select `--compare-with verilator` for the same binary input vectors with
Verilator. Choose a new `run_dir` for each execution. The
[ordinary Counter comparison](native-jit-simulation.md#compare-native-and-rtl-from-one-command)
also shows both backends without source I/O. The RTL harness evaluates its own
read guards, cursor, physical input mappings and pre-edge logs; it does not
replay native-selected reads. Icarus projects diagnostic U to X. The comparison
retains the public requirement for known, equal functional outputs and controls; it does not prove four-state algebraic equivalence.
Comparison requires file-bound output channels and retains failure/tool logs
when an artifact directory is supplied. It is differential execution, not an
unbounded formal proof. Normal synthesis and formal DUT products omit the block.

## Python and VS Code

```python
import json
from pathlib import Path
from zlang import sim

rows = [json.loads(line) for line in Path("examples/simulation_io.events.jsonl").read_text().splitlines() if line.strip()]
events = [{key:value for key,value in row.items() if key != "repeat"}
          for row in rows for _ in range(row.get("repeat", 1))]
program = sim.compile("examples/simulation_io.zhl", top="SimulationIoExample")
with program.create(
    simulation="vectors",
    io_inputs={"stimulus": "examples/simulation_io.vectors.jsonl"},
    io_outputs={"results": "build/python-samples.jsonl"},
) as dut:
    dut.run_events(events)
status = dut.simulation_io
```

A VS Code launch adds `simulation`, `simulationInputs` and `simulationOutputs`
to its normal `events` or bounded `clock`/`cycles` configuration. The checked-in
**Debug simulation I/O** launch uses console output so switching examples does
not collide with an old file. These properties require a source-matched extension,
LSP and native adapter supporting debug overlay v4; old v3 launches remain valid.
Restart rewinds the frozen vectors and hardware schedule, appending a new epoch
to the same logs rather than overwriting earlier evidence. Starting a separate
session with file outputs requires new paths. Python simulation remains a
separate instance from the source debugger/live waveform.
