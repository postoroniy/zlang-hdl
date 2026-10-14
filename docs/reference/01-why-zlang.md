<a id="reference-why-zlang"></a>
## Why ZLang HDL

ZLang HDL is a typed hardware description language and compiler for designs whose numerical, state, timing, and protocol meaning must remain explicit. The production backend emits Direct SystemVerilog from checked semantics. Unsupported combinations are errors; they are not silently reinterpreted as different hardware.

### What makes ZLang different

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

### From intent to hardware

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

### Core principles

- Width and signedness changes are explicit with operations such as `extend`, `truncate`, and `bitcast`.
- Fixed-point rounding and overflow are explicit at `quantize` boundaries.
- Clock, reset, domain ownership, state updates, and protocol transfers have exact cycle meaning.
- Rules commit atomically from one pre-edge snapshot.
- Protocol adaptation and clock-domain crossing are explicit.
- Compilation and published artifacts are deterministic for the same inputs.
- Unsupported forms stop with a diagnostic.
