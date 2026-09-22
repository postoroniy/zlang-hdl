# Language authoring and production RTL

Use this reference for `.zhl` source, stdlib, hierarchy, protocols, state,
storage, numeric behavior, simulation, or direct-SystemVerilog integration.

## Current source model

- `.zhl` is the canonical suffix; `.zl` is rejected.
- Hardware assignments are concurrent. `=` drives now and `<-` schedules one
  atomic next-edge update from the immutable pre-edge snapshot.
- Ordinary body bindings are immutable, declaration-ordered, and may infer
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

Prefer source-authored stdlib declarations under `stdlib/**/*.zhl`; buses are
ordinary typed modules/protocols, never backend name dispatch. Logical imports
such as `import std.bus.ahb_lite` and qualified imports must resolve through the
project/compiler resolver rather than relative filesystem guesses.

## State, hierarchy, and protocols

- `clock clk reset rst` is the default synchronous domain.
- `async reset arst @clk` means asynchronous assertion with fixed two-edge
  synchronized release. Do not emulate reset policy in datapath source.
- FIFO, memory, ROM, register, CSR, and rule semantics include explicit reset,
  latency, ownership, conflict, and collision behavior.
- Use typed `child : Module`/`inst child : Module`, explicit bindings, and
  explicit `connect`/`source -> sink`, adapters, and CDC crossings.
- `rv<T>`, `credit<T,N>`, request/response, and bus observations are protocol
  semantics, not arbitrary struct fields.
- Runtime-selected child output is a projection over already existing physical
  children; it does not select which child executes.

The public top ABI recursively exposes struct and tuple leaves and preserves
vectors as native unpacked arrays. Use BackendArtifact logical bindings; never
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

Read these authoritative topic guides as needed:

- `docs/types-and-numerics.md`
- `docs/expressions-functions-generics.md`
- `docs/sequential-state-storage.md`
- `docs/hierarchy-protocols.md`
- `docs/projects-dependencies.md`
- `docs/direct-systemverilog.md`
