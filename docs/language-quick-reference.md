# ZLang HDL concise source-authoring reference

This page is a concise guide to writing and running ZLang HDL. Source-language
rules define the hardware; generated SystemVerilog is an output format, not a
second language specification.

Full details: [Language Reference](language-reference.md),
[Standard Library Guide](stdlib.md), [User Guide](user-guide.md).

For an unfamiliar construct, consult the detailed language chapter and
[known limitations](language-reference.md#reference-known-limitations). For
exact width tables and supported forms, follow the linked topic guide.

## Non-negotiable rules

- Source files use `.zhl`. The old `.zl` suffix is rejected.
- Run `zlang SOURCE --check`; highlighting is lexical and is not validation.
- Native simulation needs a matching platform wheel and fails explicitly when
  it is unavailable.
- Combinational connections such as `y = a + b` are continuously defined;
  state updates such as `count <- next_count` schedule next state at the active
  clock edge. The `=` token also appears in immutable bindings and initializers;
  it does not imply a state update.
- Assignment types are exact. There is no implicit resize, signedness change,
  fixed-point rescale, protocol adapter, or clock-domain crossing.
- ZLang has no `let`, `const`, `var`, `return`, semicolons, C-style
  `input`/`output`, or runtime procedural `if` syntax.
- `if` is compile-time elaboration only. Use `?:`, `mux`, or `switch` for pure
  runtime values; use `when`/`else when`/`else`, `priority`, or `fsm` for state
  actions.
- Scalar `pipeline(auto)`, `architecture(auto)`, and `explore` are retired.
  Compiler-selected scalar implementation uses `implement`.
- `pipeline(N) { expression }` is exact N-cycle hardware, not an optimization
  request. Protocol `transform pipeline(auto, ...)` is a separate bounded
  ready/valid construct and remains supported.
- Unsupported combinations fail with a diagnostic. Generated signal names are
  not a source-language interface.

## Smallest valid modules

For buffered streams and numerical components, see
[stream composition, CRC and FIR](stdlib-stream-dsp.md):
`std.stream.composition` provides `RvFork2`/`RvJoin2`, `std.coding.crc`
provides `RvCrc32Byte`, and `std.dsp.fir` provides exact and explicitly
quantized parallel FIR. FIR has one elastic output stage, II=1, immutable
coefficients, and history that advances only on `input.transfer`.

[`std.dsp.nco`, `std.dsp.nco_linear`, `std.dsp.cordic`, `std.dsp.cic`,
`std.dsp.polyphase`, `std.dsp.halfband` and coding blocks](stdlib-multirate-coding.md)
provide explicit accepted-sample phase, decimation/interpolation, wrap/quantization and bit-order
contracts. These are source-authored stdlib components, not new language syntax.
For [vectoring CORDIC](stdlib-multirate-coding.md#vectoring-atan2-and-magnitude)
and [atomic FIR reload and one-MAC folded FIR](stdlib-reloadable-fir.md), use
their separate numerical, coefficient-epoch and latency/II contracts.

Additional source-authored modules:

- [SECDED](stdlib-secded.md): K=1…64, single-bit correction, double-bit
  detection, one elastic stage.
- [Farrow](stdlib-farrow.md): M=0…3, K=1…8, raw unsigned μ, exact stored
  coefficients, M+1 edges and optional final-only quantization.
- [DVB RS(204,188)](stdlib-rs-dvb.md): framed systematic encoder and errors-only
  decoder, up to eight erroneous symbols; status before payload.
- [K=7 Viterbi](stdlib-viterbi.md): hard/3-bit-soft pairs, full-frame traceback,
  deterministic ties, no assumed zero final state or implicit tail bits.

These limits describe the source contracts, not universal proof coverage or
the contents of an older installed package. See each guide's status and example.

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

Multiple domains use the same declarations with one `@clock` owner:

```zlang
module DualClock {
    clock a reset ra @a
    clock b reset rb @b
    reg left : u32 @a = 0
    reg right : u32 @b = 0
    rule TickLeft @a when 1 { left <- truncate<32>(left + 1) }
    rule TickRight @b when 1 { right <- truncate<32>(right + 2) }
}
```

An omitted owner is accepted only when exactly one domain can be inferred.
Dynamic provenance follows through local expressions; an explicit
`sync_level`, `pulse_toggle`, `handshake`, or `async_fifo(N)` is the only way
to cross domains.

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
- `quantize<T>` provides explicit fixed-point rounding/overflow. Its placement
  is part of the computation and cannot be moved into products or reductions.
- Compile-time `pi`, `sin`, `cos`, `log2`, `log`, `exp`, and `sqrt` generate
  constants only; non-integral results require explicit `quantize<T>`.
- Use `bitcast<T>` only for equal-width representation changes. `pack`/`unpack`
  are supported low-level compatibility forms.
- `concat(a,b,...)` places the first operand at the MSB. Destination context
  never changes operand widths.
- Packed bit index zero is the LSB. `x[MSB:LSB]` uses an inclusive static slice;
  vector ranges `values[first..past_last]` are half-open.
- Indexed aggregates are packed LSB-first: `vec`/`string` element zero and tuple
  `item0` occupy the least-significant component recursively. First-declared
  struct fields and tagged-union tags remain at the MSB.

See [types and numerics](language-reference.md#reference-types-and-numerics) before changing a width or a
fixed-point type.

### Operators at a glance

- Arithmetic: `+`, binary `-`, `*`, unary `-`. Built-in integer families do
  not mix signed and unsigned operands implicitly. For example, `u8 + u8`
  is `u9`, `u8 - u8` is modular `u8`, `s8 - s8` is `s9`, and `s8 * s8` is
  `s16`. Assignment does not narrow any of these results.
- Bitwise: `&`, `|`, `^`, `~`; logical `!` requires `bit`. Runtime Boolean
  control combines `bit` values with `&` and `|`, not `&&` and `||`.
- Shifts: `<<`, `>>` preserve the left operand's width and require an unsigned
  shift amount. Right shift of a signed integer is arithmetic.
- Comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=` return `bit`. Ordering is
  numeric; raw bits and enums do not acquire integer ordering implicitly.
- Selection: `condition ? a : b`, `mux`, `switch`, and tagged-union `match`.
  Runtime conditions are `bit`; branches must have one exact result type.
- `/` and integer `%` are compile-time only. `&&` and `||` belong to the
  bounded compile-time/guard sublanguages, not runtime hardware truthiness.

See [operator precedence and typing](language-reference.md#reference-expressions-functions-generics-operators-and-selection)
for mixed widths, fixed-point rules and overload resolution.

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

Struct updates use `value with { field = replacement }`, not a second struct
constructor. Enum backing may be `bit`, `bits<W>` or an unsigned integer type.
Omitted member codes start at zero or increment the preceding explicit code.
Use `Enum.Member`, exhaustive `switch`, and `enum_encode` for a numeric
boundary; raw encodings use `enum_valid<T>` and total `enum_decode<T>` with a
same-enum fallback. A raw-bit encoding needs an explicit numeric `bitcast`
before arithmetic. Top-level enum inputs and arbitrary `unpack<Enum>` are not
supported. Enum members such as `reserved`, `reset` and `priority` are valid
contextual identifiers; lexical highlighting is not a reserved-name rule.

Generics are monomorphized. Type/value parameters and exact compile-time
constraints are source declarations, not runtime hardware:

```zlang
module Queue<type T,D=4> where D >= 2 && is_power_of_two(D) {
    // ...
}
```

Integer `%` is compile-time only: it may appear in generic arguments, static
indices and compile-time `if`. It has the precedence of multiplication and
division and uses floor modulo (`-5 % 3 == 1`, `5 % -3 == -1`). An evaluated
zero divisor is an error.
Ports, registers and ordinary function arguments are not compile-time operands;
there is no runtime remainder. See the
[complete contract and PSS example](compile-time-integers.md).

Imports are logical. For example `import std.bus.reg` resolves compiler-shipped
`stdlib/bus/reg.zhl`; `import std.math.complex as cx` adds a source-local
qualifier but does not create a runtime namespace.

## State, hierarchy, and protocols

- Declare one default synchronous domain with `clock clk reset rst`.
- `async reset arst @clk` means asynchronous assertion and fixed two-edge
  synchronized release. Reset modes are expressed by the clock/reset
  declarations rather than user logic.
- Add `release externally_synchronized` only when deassertion is already
  synchronized outside; no internal 2FF is emitted.
- `reg q:T = VALUE` resets to `VALUE`; `reg q:T` has no reset branch.
- `out q:T` is a combinational output wire.
- `out reg q:T [@clock] [= VALUE]` is real architectural state: it holds when
  not updated and follows register clock/reset scheduling. It is not Verilog's
  historical `output reg` declaration category.
- `drive q = expression` is a transient rule/FSM-owned logical wire effect,
  zero when no owning action fires. It is not tri-state, high-impedance,
  electrical-strength, pad, analog, or multiple-driver-resolution behavior.
- `reg`, `fifo`, `memory`, and `rom` retain explicit typed state/storage
  semantics. Memory latency, collision, mask, and reset policies are not inferred.
- A concise child declaration is `child : ChildModule`; `inst child : ChildModule`
  remains accepted. Bind only typed ports/endpoints.
- `rv<T>`, `credit<T,N>`, and `request_response` properties such as `.transfer`
  are typed observations, not ordinary struct fields.
- Use explicit `connect`/`source -> sink`, adapters, and CDC crossings. The
  compiler never inserts these silently.
- Top-level structs and tuples become recursively named leaf ports; vectors use
  multidimensional packed arrays in the production Direct-SV ABI.

Read [sequential state/storage](language-reference.md#reference-sequential-state-storage) and
[hierarchy/protocols](language-reference.md#reference-hierarchy-protocols) before composing stateful children.

## Functional datapath and implementation intent

`generate`, `map`, `reduce`, `sum`, and `dot` are statically bounded functional
hardware expressions. They are not runtime loops.

Common built-ins by purpose:

- Width/representation: `extend`, `truncate`, `bitcast`, `pack`, `unpack`,
  `concat`, `reshape`, `zeros<N>`, `ones<N>`, and `parity`.
- Fixed point: `quantize<T>`, `fixed_raw<T>`, `fixed_to_raw`, and the explicit
  `fixed_truncate_*` / `fixed_round_even_*` compatibility conversions.
- Enum boundaries: `enum_encode`, `enum_valid<T>`, `enum_decode<T>`.
- Compile-time shape: `length`, `floor_log2`, `ceil_log2`, `index_width`,
  `is_power_of_two`; compile-time numerical constants also use `pi`, `sin`,
  `cos`, `log2`, `log`, `exp`, and `sqrt`.
- Collections: `generate`, `map`, `reduce`, `sum`, `dot`, and `repeat`.
- Timing: `delay<N>(value)` and `pipeline(N) { value }` add exact cycles;
  they are not ordinary pure-function calls or architecture-search requests.

These are context-sensitive operations, not a promise of arbitrary operand
types. Use the linked type/expression chapters for exact signatures. Imported
stdlib helpers and user-defined functions are separate from compiler intrinsics.

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

An Fmax constraint or objective compares available implementation choices. It
is not a guarantee of achieved device or silicon frequency; estimates,
synthesis measurements, and routed measurements are distinct.

The expression is the hardware meaning. `intent` contains hard constraints and
one supported objective; it does not name optimization passes. Search is
bounded and deterministic rather than an exhaustive optimality guarantee.
Positive-latency choices require a valid clock/reset context. `choice` is
reserved for user-authored equivalent alternatives. Project profiles may set
the documented `intent-exploration` count limits; there is no source-level
timeout.

See [implementation intent, frequency, and II](language-reference.md#reference-implementation-intent)
for every accepted metric/objective, synthesis clock control, project-profile
configuration, and the exact ready/valid II>1 boundary.

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

Liveness, arbitrary SVA/SMT, temporal sequences, and source-level equivalence
controls are outside this syntax. Start from the existing
[formal examples](../examples/verification/README.md).

## Commands and completion check

```sh
zlang source.zhl --check
zlang source.zhl --top Top --systemverilog build/Top.sv
zlang source.zhl --top Top --target sky130-fd-sc-hd --systemverilog build/Top.sv
zlang source.zhl --top Top --verify
zlang sim source.zhl --top Top --clock clk --cycles 100
zlang sim source.zhl --top Top --logic-state --clock clk --cycles 100
zlang sim source.zhl --top Top --strict-uninitialized --clock clk --cycles 100
zlang verify build/verify --mode bmc --depth 20 --work-dir build/verify-work
```

Ordinary project compilation validates the checked-in lock read-only. Every
`zlang.toml` project requires `zlang.lock`, including zero-dependency projects.
Restore the checked-in lock when cloning; for a new project without one, create
it explicitly with `zlang lock update --project zlang.toml`. Thereafter run that
command only after dependency declarations, dependency source contents, exact
Git revisions or other resolution inputs change, not before each simulation or
RTL build. Compilation never fetches or rewrites the lock. Standalone sources
without a manifest need no lock. See [project setup](projects-dependencies.md#reference-projects-dependencies).

In a `--events` JSONL schedule, `"repeat": N` repeats one event lazily for a
bounded positive number of steps (at most 1,000,000 expanded events) without a
project-specific generator.

Use `--cycles` only for a one-clock top that does not need an explicit reset
sequence. Resettable or multi-clock designs should use JSONL so pin levels,
inputs and coincident edges are reproducible:

```jsonl
{"reset":{"rst":true},"edges":["clk"]}
{"reset":{"rst":false},"set":{"enable":1},"edges":["clk"]}
{"set":{"enable":0},"edges":["clk"],"repeat":4}
```

Run it with `--events events.jsonl`; add `--trace build/run.fst` and repeated
`--trace-signal NAME` options for a selective waveform. Add
`--compare-with verilator` or `--compare-with iverilog` to replay the same
events against generated Direct-SV. Any mismatch, unresolved RTL output, tool
failure, or timeout is an error, not a warning.

Ordinary native simulation retains its deterministic two-state zero seed.
`--logic-state` enables per-bit `0`/`1`/`U`/`X`: unreset state starts as `U`,
an accepted unresolved write becomes `X`, and controlling values such as
`U | 1` still resolve exactly. `--initial-reg` overrides selected initial
registers and `--trace-signal` selects waveform signals. Prefer `.fst` for
native interactive traces; `.vcd` remains supported for interchange. Hidden
`__zlang_meta/*_u_mask` traces distinguish `U` from the formats' shared `x`
spelling.
`--strict-uninitialized` implies this mode and rejects observed `U` or `X`.
Simulation executes the selected implementation in both modes. U/X tags are
diagnostics, not physical values; legal binary-equivalent optimization may
change their propagation. Strict mode checks typed observations and stateful
controls, not every syntactic operand in the original expression. Traces and
`get_logic()` may retain unresolved tags for inspection.
Neither mode models `Z`, electrical resolution, or Verilog event regions.
CLI FST/VCD output is an event-index visualization: physical clock/reset levels
match each compiled domain, but the JSONL schedule does not define analog time.
