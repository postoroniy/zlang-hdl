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

Read [types and interfaces](types-and-interfaces.md) before choosing a numeric,
aggregate, storage, or protocol type. It contains the current type catalogue,
exact manipulation rules, endpoint ownership, standard-bus selection, and
runnable witnesses. The invariant here is simple: width, signedness, fixed
scale, nominal identity, shape, role, and clock domain never change implicitly.

## Functions, collections, and generics

Functions are pure and combinational. They cannot capture module ports or own
state/protocol/timing. Generic functions/modules are monomorphized by exact
type/value/callable arguments. `generate`, `map`, `reduce`, `sum`, and `dot` are
compile-time-bounded hardware expressions, not runtime loops.

Prefer source-authored modules from the built-in `std` namespace; buses are
ordinary typed modules and protocols, never backend name dispatch. Select the
bus profile and example through
[types and interfaces](types-and-interfaces.md). Logical imports such as
`import std.bus.ahb_lite` and qualified imports resolve through the language's
module system rather than relative filesystem guesses.

## State, hierarchy, and protocols

- `clock clk reset rst` is the default synchronous domain.
- `async reset arst @clk` means asynchronous assertion with fixed two-edge
  synchronized release. Do not emulate reset policy in datapath source.
- FIFO, memory, ROM, register, CSR, and rule semantics include explicit reset,
  latency, ownership, conflict, and collision behavior.
- Use typed `child : Module`/`inst child : Module`, explicit bindings, and
  explicit `connect`/`source -> sink`, adapters, and CDC crossings.
- `rv<T>`, `credit<T,N>`, packet, request/response, and bus observations are
  protocol semantics, not arbitrary struct fields. Endpoint ownership and
  concrete examples are in [types and interfaces](types-and-interfaces.md).
- Runtime-selected child output is a projection over already existing physical
  children; it does not select which child executes.

The public top ABI recursively exposes struct and tuple leaves and preserves
vector leaves as multidimensional packed SystemVerilog arrays. Use the emitted
artifact's logical bindings and source map; never guess flattened signal names.

## Compile and validate

```sh
.venv/bin/zlang source.zhl --top Top --check --diagnostic-format json
.venv/bin/zlang source.zhl --top Top \
  --systemverilog build/Top.sv --verilator-lint --verbose
```

For project work, inspect `zlang.toml`/`zlang.lock` and pass `--project` or
`--profile` when the design requires them. Compilation is offline/non-mutating;
dependency fetching belongs to an explicit lock update, not ordinary compile.

Use `docs/language-reference.md` as the authoritative topic guide, especially
its chapters on types and numerics, expressions and generics, sequential state
and storage, hierarchy and protocols, projects and dependencies, and Direct-SV.
