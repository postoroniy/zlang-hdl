<a id="reference-implementation-intent"></a>
# Implementation intent, frequency, and II

Use `implement` when an expression defines one exact hardware value but more
than one legal implementation may exist. The expression remains the required
result. The nested `intent` block filters legal choices and optionally states
which cost to prefer:

```zlang
module IntentDotProduct {
    clock clk reset rst
    in a : vec<4,u3>
    in b : vec<4,u3>
    out y : u8

    y = implement {
        dot(a, b)
        intent {
            latency <= 3
            ii == 1
            dsp <= 4
            fmax >= 100
            minimize latency
        }
    }
}
```

The compiler keeps the original exact expression as a legal choice and may
also consider a bounded set of equivalent structures and physical
implementations. It never relaxes a hard constraint. If no candidate satisfies
every constraint, compilation fails and reports the rejected choices.

## `intent` clauses

An `intent` block is non-empty. Clauses are separated by whitespace or newlines;
commas are not used. A metric may have at most one constraint, and the block may
have at most one objective.

| Form | Accepted values | Meaning |
| --- | --- | --- |
| `lut REL N` | `REL` is `<=`, `>=`, or `==`; `N >= 0` | Bound the LUT estimate. |
| `ff REL N` | `REL` is `<=`, `>=`, or `==`; `N >= 0` | Bound the flip-flop estimate. |
| `dsp REL N` | `REL` is `<=`, `>=`, or `==`; `N >= 0` | Bound the DSP-resource estimate. |
| `bram REL N` | `REL` is `<=`, `>=`, or `==`; `N >= 0` | Bound the block-memory estimate. |
| `latency REL N` | `REL` is `<=`, `>=`, or `==`; `N >= 0` | Bound accepted-input to corresponding-output latency according to the selected contract. A positive value requires a clock/reset context. |
| `ii REL N` | `REL` is `<=`, `>=`, or `==`; `N >= 1` | Bound initiation interval: the nominal minimum spacing between accepted transactions when downstream capacity is available. |
| `fmax REL N` | `REL` is `<=`, `>=`, or `==`; `N >= 1` MHz | Bound the available frequency estimate or compatible measurement. `fmax_est` is accepted as an alias. |
| `minimize METRIC` | `lut`, `ff`, `dsp`, `bram`, or `latency` | Rank legal choices by one cost. |
| `maximize fmax` | `fmax` or `fmax_est` | Prefer the highest compatible frequency evidence. |

If an objective is omitted, selection defaults to `minimize lut`. Frequency may
only be maximized; resources and latency may only be minimized. II has no
objective form. Constraints filter the available architectures rather than
creating new ones: for example, `ii <= 4` may select an existing II=1 choice.

`pipeline(N) { expression }` is different from `implement`. It always specifies
exactly `N` cycles of visible latency. An `implement` block instead allows the
compiler to select among the existing legal choices admitted by its constraints.
Use `choice(...)` when the source explicitly supplies alternative expressions.

## Frequency control and synthesis trials

Frequency selection and the physical synthesis clock are separate controls:

| Control | Purpose |
| --- | --- |
| `fmax >= 100` inside `intent` | Reject an implementation choice whose admitted frequency value is below 100 MHz. |
| `maximize fmax` | Rank the remaining legal choices by their admitted frequency values. |
| `fmax = { minimum = 100 }` in a selected project profile | Apply the same minimum to profile-selected regions. |
| `period-ns = 10.0` under the profile clock table | Emit a real 10 ns XDC/SDC clock constraint for downstream synthesis and place-and-route. |
| `evidence-policy` | Decide whether estimates are accepted, measurements are preferred, or measurements are required. |

For a target of `F` MHz, the corresponding physical period is `1000 / F` ns.
Setting only `fmax >= F` does not create a clock constraint and does not prove
timing closure. Conversely, a clock period constrains the downstream tool but
does not guarantee that the design meets it.

A project profile can keep these settings together:

```toml
[profiles.synth_trial]
backend = "systemverilog"
backend-mode = "required"
target = "xc7z030ffg676-1"
objective = "maximize fmax"
evidence-policy = "measured_preferred"
formal-policy = "available"

[profiles.synth_trial.constraints]
latency = { maximum = 4 }
ii = { exact = 1 }
fmax = { minimum = 100 }
dsp = { maximum = 8 }

[profiles.synth_trial.platform.clocks.clk]
period-ns = 10.0
```

Emit SystemVerilog and matching constraints from that profile:

```sh
zlang src/top.zhl --project zlang.toml --profile synth_trial \
  --systemverilog build/Top.sv \
  --constraints-xdc build/Top.xdc \
  --constraints-sdc build/Top.sdc \
  --exploration-report build/implementation.txt
```

Supply `Top.sv` and the appropriate constraint file to the FPGA/ASIC synthesis
flow. ZLang does not silently launch a vendor tool. For a bounded generic Yosys
characterization trial, request both a report and cache:

```sh
zlang src/top.zhl --project zlang.toml --profile synth_trial \
  --systemverilog build/Top.sv \
  --synthesis-report build/yosys-synthesis.txt \
  --synthesis-cache build/yosys-cache \
  --synthesis-target generic-lut6
```

`estimate_only` uses estimates. `measured_preferred` ranks compatible routed
measurements ahead of synthesis measurements and estimates, but still permits
an estimate when no compatible measurement exists. `measured_required` rejects
a choice when the required compatible measurement is absent. Measurements are
specific to their target, part, tool, recipe, constraints, and design; a result
from a different trial is not silently reused.

## Ready/valid contracts and II

For a ready/valid boundary, transactions occur only on transfers:

```text
input transfer  = input.valid  && input.ready
output transfer = output.valid && output.ready
```

Latency is the unstalled distance from an accepted input to its corresponding
valid output. II is the nominal minimum spacing between accepted inputs when
the implementation and downstream path have capacity. Capacity is the number
of accepted but not yet retired transactions the block can hold. Downstream
backpressure may increase the observed wall-clock interval; it does not change
the declared II.

Current support has three distinct cases:

| Form | Current behavior |
| --- | --- |
| Ordinary scalar `implement` | Fixed-rate only. Available candidates have II=1; an II>1 constraint does not create resource sharing. |
| `transform pipeline(auto, ...)` over a supported ready/valid kernel | Globally stalled pipeline with unstalled II=1. Output data and `valid` hold while downstream `ready` is low. |
| `transform pipeline(auto) { implement { a*b + c*d ... } }` | Narrow non-interleaved shared arithmetic: one multiplier, latency 4, II 4, capacity 1. |

The supported II=4 form requires one ready/valid input and output, integer
operands, the exact `a*b + c*d` shape, one clock/reset domain, `dsp <= 1`, and
an intent that admits II 4 and latency 4:

```zlang
struct SharedOperands {
    a : u16
    b : u16
    c : u16
    d : u16
}

module SharedMultiply {
    clock clk reset rst
    in input : rv<SharedOperands>
    out output : rv<u33>

    input -> output {
        transform pipeline(auto) {
            implement {
                input.payload.a * input.payload.b
                    + input.payload.c * input.payload.d
                intent {
                    latency <= 4
                    ii == 4
                    dsp <= 1
                    minimize dsp
                }
            }
        }
    }
}
```

Only one transaction is active. While computation or an unconsumed result is
present, new input is blocked. When `output.valid && output.ready` retires a
result, the block may accept the next input on the same edge; this is why the
`RETIRE_AND_RELOAD` schedule has II 4 rather than II 5. A stalled output
remains valid with stable payload, and input readiness remains low until
retirement is possible.

This is not general HLS. It does not support overlapping transactions, modulo
scheduling, arbitrary arithmetic graphs, more products, fixed-point temporal
sharing, memories, CDC, or multiple active transactions. The transaction-stream
formal route supplies bounded model-checking evidence for this exact case.
`required_bmc` may accept it only after `bounded_pass`; `required_proven`
rejects it because no unbounded proof is claimed.

The runnable example is
[`examples/temporal_shared_multiply.zhl`](../examples/temporal_shared_multiply.zhl).
