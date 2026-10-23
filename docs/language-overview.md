<a id="reference-why-zlang"></a>
# Why ZLang HDL

ZLang HDL is a typed hardware description language and compiler for designs whose numerical, state, timing, and protocol meaning must remain explicit. The production backend emits Direct SystemVerilog from checked semantics. Unsupported combinations are errors; they are not silently reinterpreted as different hardware.

## What makes ZLang different

| Concern | ZLang approach |
| --- | --- |
| Widths | Exact; no silent resize |
| Fixed point | Explicit quantization |
| State | Explicit next-state and atomic actions |
| Timing | Exact latency contracts |
| CDC | Explicit semantic crossing |
| Implementation choice | `implement` plus `intent` |
| Optimization | Exact legal alternatives only |
| Physical mapping | Target- and resource-aware |
| Verification | Evidence status is retained |
| RTL | Direct-SV from typed semantics |

## From intent to hardware

```text
Designer intent
      ↓
Exact typed semantics
      ↓
Exact implementation alternatives
      ↓
Target/resource planning
      ↓
Verification and evidence
      ↓
Direct SystemVerilog
      ↓
FPGA / ASIC toolchain
```

Source defines required behavior. The compiler may vary structure only through exact alternatives admitted by the language and selected profile. Timing and state contracts remain part of the design. Resource estimates, synthesis measurements, routed measurements, bounded checks, and proofs retain distinct evidence labels.

## Core principles

- Width and signedness changes are explicit with operations such as `extend`, `truncate`, and `bitcast`.
- Fixed-point rounding and overflow are explicit at `quantize` boundaries.
- Clock, reset, domain ownership, state updates, and protocol transfers have exact cycle meaning.
- Rules commit atomically from one pre-edge snapshot.
- Protocol adaptation and clock-domain crossing are explicit.
- Compilation and published artifacts are deterministic for the same inputs.
- Unsupported forms stop with a diagnostic.

<a id="reference-quick-start"></a>
## Quick start: one complete design

The following design combines exact arithmetic, explicit state, an atomic rule, a safety property, and bounded implementation selection.

```zlang
module RunningSum {
    clock clk reset rst
    in enable : bit
    in a, b : u8
    out reg total : u10 = 0
    out pair_sum : u9

    pair_sum = implement {
        a + b
        intent { latency == 0 ii == 1 minimize lut }
    }

    accumulate: when enable {
        total <- truncate<10>(total + extend<10>(a + b))
    }

    assert pair_sum_is_exact @ clk {
        extend<10>(pair_sum) == extend<9>(a) + extend<9>(b)
    }
}
```

In `pair_sum = ...`, the `=` assignment defines a combinational connection; it does not update a register. Addition produces its exact result width, so two `u8` operands produce `u9`. The state update explicitly extends the sum and truncates the accumulated result to ten bits: `total` wraps modulo 1024 rather than reporting overflow. The `<-` update schedules the register's next value at the active clock edge. The rule reads one pre-edge snapshot and commits atomically. The `out reg` declaration creates architectural storage that holds its value when no update occurs.

The expression inside `implement` defines the required value. Its `intent` constrains legal implementations without changing that value. Here the result is combinational (`latency == 0`) with initiation interval one (`ii == 1`). The assertion checks the selected `pair_sum` against an independently widened addition; it is not an overflow check on the wrapping accumulator.

### Source files and notation

A source file may contain imports, type aliases, structs, pure functions, operator overloads, rewrite declarations, target/resource declarations, and multiple modules. Select a top explicitly with `--top` when needed. Names are case-sensitive; there are no semicolons.

| Form | Meaning |
| --- | --- |
| `name = expression` | Define a value, combinational connection, or resource control, according to the declaration. |
| `state <- expression` | Schedule next state at the active edge. |
| `source -> destination` | Connect typed endpoints or bind a command. |
| `key => expression` | Select a value arm. |
| `thing @ clk` | Associate a declaration with a clock domain. |
| `object.field` | Select an aggregate, protocol, or resource member. |
