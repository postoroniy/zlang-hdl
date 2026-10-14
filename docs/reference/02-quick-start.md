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

    assert total_is_bounded @ clk { total <= 1023 }
}
```

`=` drives combinational hardware. Addition produces its exact result width, so two `u8` operands produce `u9`; a wider state update uses explicit `extend` and `truncate`. `<-` schedules next state on the active edge. The rule reads one pre-edge snapshot and commits atomically. `out reg` is architectural storage and holds its value when no update occurs.

The `implement` expression is the required value. Its `intent` constrains legal implementations without changing that value. Here the result is combinational (`latency == 0`) and accepts a new value every cycle (`ii == 1`).

Check, simulate, verify, and generate production RTL:

```sh
zlang design.zhl --top RunningSum --check
zlang sim design.zhl --top RunningSum --events events.jsonl --trace build/run.fst
zlang design.zhl --top RunningSum --verify --verification-work-dir build/verify
zlang design.zhl --top RunningSum --systemverilog build/RunningSum.sv
```

The generated Direct-SV is the production RTL artifact. Native simulation runs the checked design without generating RTL first; differential comparison can replay the same event stream against Icarus or Verilator. Verification reports preserve whether a result is bounded evidence or an unbounded proof.

### Source files and notation

A source file may contain imports, type aliases, structs, pure functions, operator overloads, rewrite declarations, target/resource declarations, and multiple modules. Select a top explicitly with `--top` when needed. Names are case-sensitive; there are no semicolons.

| Form | Meaning |
| --- | --- |
| `name = expression` | Drive a combinational value or resource control. |
| `state <- expression` | Schedule next state at the active edge. |
| `source -> destination` | Connect typed endpoints or bind a command. |
| `key => expression` | Select a value arm. |
| `thing @ clk` | Associate a declaration with a clock domain. |
| `object.field` | Select an aggregate, protocol, or resource member. |
