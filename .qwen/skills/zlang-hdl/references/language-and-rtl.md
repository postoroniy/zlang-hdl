# Language authoring and production RTL

Use this reference for `.zhl` source, stdlib, hierarchy, protocols, state,
storage, numeric behavior, simulation, or direct-SystemVerilog integration.

## Current source model

- `.zhl` is the canonical suffix; `.zl` is rejected.
- Hardware assignments are concurrent. `=` drives now and `<-` schedules one
  atomic next-edge update from the immutable pre-edge snapshot.
- Pure-function body bindings are immutable, declaration-ordered, and may infer
  their exact type. The final function expression is its result.
- `if` is compile-time specialization only. Use `?:`, `mux`, or `switch` for
  runtime values and atomic `when`/`priority`/`fsm` for effects.
- Source has no `let`, `const`, `var`, `return`, semicolons, or procedural HLS.

## Types and representation

Common scalars are `bit`, `u8`, `s16`, `uint<N>`, `sint<N>`, `bits<N>`, and
fixed-point types. `char` is canonical `u8`; `string<N>` is canonical
`vec<N,u8>`. Structs/enums/unions are nominal; tuples are structural.

Arithmetic, assignment, and fixed-point conversions are exact. Use:

- `extend<N>`/`truncate<N>` for explicit width change;
- `quantize<T>` for explicit fixed rounding and overflow;
- `bitcast<T>` only for equal-width representation change;
- `concat(a,b,...)` with the first operand at the MSB;
- `x[MSB:LSB]` for inclusive static packed slices.

Do not infer numeric conversion from a successful raw equal-width boundary. The
only implicit representation exception is an explicitly typed equal-width
boundary where one side is `bits<N>` or flat `vec<N,bit>`.

## Functions, collections, and generics

Functions are pure and combinational. They cannot capture module ports or own
state/protocol/timing. Generic functions/modules are monomorphized by exact
type/value/callable arguments. `generate`, `map`, `reduce`, `sum`, and `dot` are
compile-time-bounded hardware expressions, not runtime loops.

Integer `%` is evaluated only during elaboration: use it with integer
constants, specialized value parameters and generated indices, including in
generic arguments and compile-time `if`. It has the precedence of `*`/`/`
and is left-associative. Floor modulo gives `-5 % 3 == 1` and `5 % -3 == -1`.
An evaluated zero divisor is a source error; an unselected compile-time branch
is not evaluated. Ports, registers and ordinary function parameters remain
runtime values even when initialized or called with constants. There is no
runtime or fixed-point remainder operator. See
[`docs/compile-time-integers.md`](../../../../docs/compile-time-integers.md)
for a complete PSS sequence example.

Prefer source-authored stdlib declarations under `stdlib/**/*.zhl`; buses are
ordinary typed modules/protocols, never backend name dispatch. Logical imports
such as `import std.bus.ahb_lite` and qualified imports must resolve through the
project/compiler resolver rather than relative filesystem guesses.

For `std.stream.composition`, `std.coding.crc` and `std.dsp.fir`, follow the
shared [stream/DSP guide](../../../../docs/stdlib-stream-dsp.md). It owns
buffering, frame/accepted-sample ordering, immutable coefficient arguments,
exact FIR widths and the single final conversion. Reuse these ordinary
modules rather than introducing name-specific compiler paths.

Use the [multirate/numerical guide](../../../../docs/stdlib-multirate-coding.md)
for rotation/vectoring CORDIC, ROM/interpolated NCO, CIC, polyphase and halfband
filters, and 24-bit coding. Use the
[reload/folded FIR guide](../../../../docs/stdlib-reloadable-fir.md) for atomic
coefficient epochs and explicit one-MAC latency/II=N. Samples/history advance
on accepted transfers; preserve exact widths and the single final conversion.
These modules neither expand `implement` nor perform implicit CDC.

Use the shared [SECDED](../../../../docs/stdlib-secded.md),
[Farrow](../../../../docs/stdlib-farrow.md),
[DVB Reed–Solomon](../../../../docs/stdlib-rs-dvb.md) and
[Viterbi](../../../../docs/stdlib-viterbi.md) contracts for the newer coding
and delay components. SECDED guarantees only single-bit correction/double-bit
detection. Farrow uses stored binary coefficients and raw unsigned μ, with
final-only quantization. RS is specifically shortened (204,188), GF polynomial
0x11D, roots α⁰…α¹⁵, errors-only correction up to eight symbols. Viterbi uses
the existing encoder recurrence/wire order and never assumes a zero final state.
Honor framed status/data validity; source availability and mathematical
contracts do not establish executed formal evidence or refreshed artifacts.

## State, hierarchy, and protocols

- `clock clk reset rst` is the default synchronous domain.
- `async reset arst @clk` means asynchronous assertion with fixed two-edge
  synchronized release. Do not emulate reset policy in datapath source.
- FIFO, memory, ROM, register, CSR, and rule semantics include explicit reset,
  latency, ownership, conflict, and collision behavior.
- `out name:T` is combinational. `out reg name:T [@clock] [= reset]` is
  output-visible state updated with `<-`; `drive name = value` is the explicit
  transient action for an ordinary output inside a rule or FSM.
- Use typed `child : Module`/`inst child : Module`, explicit bindings, and
  explicit `connect`/`source -> sink`, adapters, and CDC crossings.
- `rv<T>`, `credit<T,N>`, request/response, and bus observations are protocol
  semantics, not arbitrary struct fields.
- Runtime-selected child output is a projection over already existing physical
  children; it does not select which child executes.

The public top ABI recursively exposes struct and tuple leaves and preserves
vector leaves as multidimensional packed arrays. Use BackendArtifact logical bindings; never
guess flattened signal names.

## Compile and validate

```sh
.venv/bin/zlang source.zhl --top Top --check --diagnostic-format json
.venv/bin/zlang source.zhl --top Top \
  --systemverilog build/Top.sv --verilator-lint --verbose
```

For project work, inspect `zlang.toml`/`zlang.lock` and pass `--project` or
`--profile` when the design requires them. Compilation is offline/non-mutating;
dependency fetching belongs to an explicit lock update, not ordinary compile.

`zlang sim` always uses the Rust native runtime; there is no reference-engine
selector and Python contains no evaluator fallback. Python prepares and
orchestrates the plan while Rust owns execution, state, reset and tracing.
Native simulation is deterministic two-state by default. `--logic-state`
enables per-bit `0`/`1`/`U`/`X`; state without an initializer begins as `U`,
while an accepted unresolved write produces `X`. Use `--initial-reg` for exact
compiler-owned register-path overrides and repeat `--trace-signal` for a
selective waveform. Prefer `.fst` for native interactive traces; `.vcd`
remains the interchange/formal compatibility format. Both represent unresolved
states as `x`, so ZLang emits hidden `__zlang_meta/*_u_mask` companions for
`U` provenance. The stricter
`--strict-uninitialized` mode implies logic-state execution and rejects an
observed `U` or `X`. These diagnostics do not model high impedance, electrical
strength or Verilog event regions and never alter Direct-SV output.

Simulation executes the selected implementation, not a second source evaluator.
Exact binary rewrites may change U/X propagation. Strict mode checks typed
observations and stateful controls, not every syntactic operand; `get_logic`
and traces can retain diagnostic tags. U/X is not an e-graph equality domain.

Use `--cycles/--clock` for a bounded simple single-clock run. Use JSONL
`--events` when reset levels, inputs, multiple clocks or coincident edges must
be explicit. Add `--trace` and repeat `--trace-signal` for FST or VCD; use
`--compare-with iverilog|verilator` to replay the same schedule against
generated Direct-SV. The comparison fails closed on mismatch, unresolved RTL,
tool failure or timeout.

Use the shared [quick reference](../../../../docs/language-quick-reference.md)
and these sections of the [language reference](../../../../docs/language-reference.md):

- [Types and conversions](../../../../docs/language-reference.md#reference-types-and-numerics)
- [Expressions and generics](../../../../docs/language-reference.md#reference-expressions-functions-generics)
- [State and storage](../../../../docs/language-reference.md#reference-sequential-state-storage)
- [Hierarchy and protocols](../../../../docs/language-reference.md#reference-hierarchy-protocols)
- [Projects and locks](../../../../docs/language-reference.md#reference-projects-dependencies)
- [RTL generation](../../../../docs/language-reference.md#reference-generate-systemverilog)
