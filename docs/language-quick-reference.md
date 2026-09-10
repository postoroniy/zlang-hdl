# ZLang HDL concise source-authoring reference

Use this page as the first context document when writing or reviewing ZLang HDL
with a coding assistant such as Qwen. It describes current executable source,
not historical proposals. The compiler's typed IR defines semantics; generated
Clash or SystemVerilog is not a second language specification.

For an unfamiliar construct, consult the
[syntax support matrix](syntax-support-matrix.md). For exact width tables and
backend limits, follow the linked topic guide rather than guessing syntax.

## Non-negotiable rules

- Source files use `.zhl`. The old `.zl` suffix is rejected.
- Run `zlang SOURCE --check`; highlighting is lexical and is not validation.
- Hardware assignments are concurrent. `=` drives the current cycle and `<-`
  schedules next state at the active clock edge.
- Assignment types are exact. There is no implicit resize, signedness change,
  fixed-point rescale, protocol adapter, or clock-domain crossing.
- Do not invent `let`, `const`, `var`, `return`, semicolons, C-style
  `input`/`output`, or runtime procedural `if`.
- `if` is compile-time elaboration only. Use `?:`, `mux`, or `switch` for pure
  runtime values; use `when`/`else when`/`else`, `priority`, or `fsm` for state
  actions.
- Scalar `pipeline(auto)`, `architecture(auto)`, and `explore` are retired.
  Compiler-selected scalar implementation uses `implement`.
- `pipeline(N) { expression }` is exact N-cycle hardware, not an optimization
  request. Protocol `transform pipeline(auto, ...)` is a separate bounded
  ready/valid construct and remains supported.
- Unsupported combinations must fail closed. Never work around a diagnostic by
  guessing backend signal names or duplicating compiler semantics in source.

## Smallest valid modules

Combinational logic:

```zlang
module Add {
    in a, b : u8
    out y : u9 = a + b
}
```

Sequential logic:

```zlang
module Accumulator {
    clock clk reset rst
    in enable : bit
    in value : u8
    out total : u9

    reg state : u9 = 0
    update: when enable {
        state <- truncate<9>(state + extend<9>(value))
    }
    total = state
}
```

All operands are read from one pre-edge snapshot. An accepted action group
commits atomically; reset has priority. Multiple conflicting actions require an
explicit `priority` relation or are rejected.

## Types, literals, and exact conversions

Common scalar types are `bit`, `u8`, `s16`, `uint<N>`, `sint<N>`, and
`bits<N>`. `char` is canonical `u8`; `string<N>` is canonical `vec<N,u8>`.
Fixed point uses `fixed<W,F>`, `ufixed<W,F>`, or concise forms such as `SF8.8`
and `SF_Sat8.8`.

- Unsuffixed integer literals use minimum exact width unless they occur directly
  at a concrete typed boundary.
- `a + b` and `a - b` follow the documented exact result-width rules;
  multiplication returns the full product width. Shifts preserve left width.
- Use `extend<N>` and `truncate<N>` for width changes.
- Use `quantize<T>` for explicit fixed-point rounding/overflow. Do not move a
  quantization boundary into products or reductions.
- Use `bitcast<T>` only for equal-width representation changes. `pack`/`unpack`
  are supported low-level compatibility forms.
- `concat(a,b,...)` places the first operand at the MSB. Destination context
  never changes operand widths.
- Packed bit index zero is the LSB. `x[MSB:LSB]` uses an inclusive static slice;
  vector ranges `values[first..past_last]` are half-open.

See [types and numerics](types-and-numerics.md) before changing a width or a
fixed-point type.

## Aggregates and functions

```zlang
struct Pair<type T> { left : T right : T }

fn duplicate_sum<type T>(a : T, b : T) {
    exact = a + b
    Pair { left = exact right = exact }
}
```

Function bodies are pure and combinational. Bare body bindings are immutable,
inferred, declaration-ordered locals; the final expression is the result.
Return types may be explicit or inferred. Functions cannot capture module ports
or contain state, protocols, delays, or pipelines.

Supported aggregates include `vec<N,T>`, nominal `struct`, nominal `enum`,
tagged `union`, and structural tuples `(T,U)`. Prefer inferred locals when the
type is exact and obvious. Keep explicit types at public ports, state, raw/enum
boundaries, narrowing, and fixed-point quantization.

Generics are monomorphized. Type/value parameters and exact compile-time
constraints are source declarations, not runtime hardware:

```zlang
module Queue<type T,D=4> where D >= 2 && is_power_of_two(D) {
    // ...
}
```

Imports are logical. For example `import std.bus.reg` resolves compiler-shipped
`stdlib/bus/reg.zhl`; `import std.math.complex as cx` adds a source-local
qualifier but does not create a runtime namespace.

## State, hierarchy, and protocols

- Declare one default synchronous domain with `clock clk reset rst`.
- `async reset arst @clk` means asynchronous assertion and fixed two-edge
  synchronized release. Do not emulate reset modes in user logic.
- `reg`, `fifo`, `memory`, and `rom` retain explicit typed state/storage
  semantics. Memory latency, collision, mask, and reset policies are not inferred.
- A concise child declaration is `child : ChildModule`; `inst child : ChildModule`
  remains accepted. Bind only typed ports/endpoints.
- `rv<T>`, `credit<T,N>`, and `request_response` properties such as `.transfer`
  are typed observations, not ordinary struct fields.
- Use explicit `connect`/`source -> sink`, adapters, and CDC crossings. The
  compiler never inserts these silently.
- Top-level structs and tuples become recursively named leaf ports; vectors stay
  native unpacked arrays in both backends.

Read [sequential state/storage](sequential-state-storage.md) and
[hierarchy/protocols](hierarchy-protocols.md) before composing stateful children.

## Functional datapath and implementation intent

`generate`, `map`, `reduce`, `sum`, and `dot` are statically bounded functional
hardware expressions. They are not runtime loops.

Use exactly one `implement` region when the value is fixed and the compiler may
choose among legal implementations:

```zlang
y = implement {
    dot(samples, coefficients)
    intent {
        latency <= 4
        ii == 1
        dsp <= 8
        fmax >= 100
        minimize lut
    }
}
```

The expression is the hardware meaning. `intent` contains hard constraints and
one supported objective; it does not name optimization passes. Positive-latency
candidates require a valid clock/reset context. `choice` is reserved for
user-authored equivalent alternatives. Egglog supplies only frozen pure scalar
value rewrites; it does not schedule pipelines or state.

See [optimization and formal](optimization-formal.md) before adding or changing
an implementation policy.

## Verification

```zlang
contract arithmetic @ clk {
    require legal_inputs { (a < 8) & (b < 8) }
    ensure exact_output { y == a + b }
    cover maximum { y == 14 }
}
```

Named `assert`/`ensure` are same-cycle safety goals. `require` is an
environment-owned precondition scoped to its contract. `cover` is bounded
reachability; a missed cover is not an unreachability proof. BMC produces
`bounded_pass`, never `proven`. Verification syntax does not legalize invalid
hardware or feed optimizer range inference.

Do not add liveness, arbitrary SVA/SMT, temporal sequences, new observation
families, or source-level M36/M38 controls. Use the existing
[formal examples](../examples/verification/README.md) and compiler-owned routes.

## Commands and completion check

```sh
zlang source.zhl --check
zlang source.zhl --top Top --systemverilog build/Top.sv
zlang source.zhl --top Top -o build/Top.hs
zlang source.zhl --top Top --verify
```

Before calling a source change complete:

1. compile the exact top with `--check`;
2. run focused parser/semantic/canonical/simulator tests;
3. exercise direct-SV with strict Verilator and real Clash when the feature
   reaches those backends;
4. run applicable existing formal tests without inventing new claims;
5. run the repository regression and `git diff --check` required by the active
   task.

If a requested form is absent from this page, check the
[syntax matrix](syntax-support-matrix.md) and [known limitations](known-limitations.md).
Do not infer support from historical milestone or design-freeze examples.
