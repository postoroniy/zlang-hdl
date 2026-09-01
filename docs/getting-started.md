# Getting started with ZLang

ZLang is a statically elaborated hardware language. Source is parsed and checked
into backend-independent typed IR before any RTL backend is selected. Clash is
the primary/general backend; direct SystemVerilog is a supported, fail-closed
secondary backend for the feature set documented in
[Direct SystemVerilog](direct-systemverilog.md).
The [current status snapshot](current-language-status.md) records the accepted
tool versions, regression/corpus counts, real-design evidence, and explicit
product boundaries.

The representative executable language tour is
[`examples/all_syntax.zl`](../examples/all_syntax.zl). It intentionally does
not enumerate every legal composition or backend boundary. The
[syntax support matrix](syntax-support-matrix.md) and compiler-owned capability
registry distinguish supported, bounded, and deferred forms.

## Install for development

This alpha release supports Python 3.12. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Check a source file without creating backend artifacts:

```sh
.venv/bin/zlangc examples/all_syntax.zl --check
```

Without `--top`, `--check` validates every declared module. With `--top NAME`,
it validates only that elaboration root. This is a semantic-only demand: it
does not run implementation planning, formal execution, report rendering, or a
Clash/SystemVerilog backend.

## A first module

```zlang
module Add {
    in  a : u8
    in  b : u8
    out y : u9

    y = a + b
}
```

Hardware assignments are concurrent. Source order does not turn `=` into
software-style sequencing. `=` drives a combinational value; `<-` schedules a
register's next value at its clock edge.

Generate Clash source:

```sh
.venv/bin/zlangc examples/extended_add.zl -o build/ExtendedAdd.hs
```

Generate and lint Clash-produced Verilog:

```sh
.venv/bin/zlangc examples/extended_add.zl \
  --verilog-dir build/extended-add-rtl --verilator-lint
```

Generate direct SystemVerilog for a supported design:

```sh
.venv/bin/zlangc examples/extended_add.zl \
  --systemverilog build/ExtendedAdd.sv
verilator --lint-only --top-module ExtendedAdd build/ExtendedAdd.sv
```

Explicit artifact paths suppress implicit Clash output on stdout. A bare
`zlangc SOURCE` retains the legacy behavior of printing Clash source. Use
`--verbose` for success messages on stderr.

## Check named verification goals

Clocked modules can carry same-cycle safety and bounded-reachability goals:

```zlang
assert count_within @ clk { count <= DEPTH }
cover reaches_full @ clk { count == DEPTH }
```

Run the applicable existing M35 safety families plus source goals:

```sh
.venv/bin/zlangc design.zl --top Top --verify \
  --formal-jobs 4 \
  --verification-report build/verification.txt \
  --verification-work-dir build/verification-work
```

Or publish a hash-validated bundle and replay it without recompiling source:

```sh
.venv/bin/zlangc design.zl --top Top \
  --verification-bundle build/verify
.venv/bin/zlang-verify build/verify --mode bmc --depth 20 \
  --work-dir build/verify-work
```

`bounded_pass` means only that no counterexample was found through the stated
depth. A cover reports `witnessed` or `bounded_unreached`; a cover miss is not a
safety failure. A `prove` request runs BMC first and starts proof only when every
safety job is `bounded_pass`. Covers run once and are not rerun; an unrelated
cover does not block proof, while an unwitnessed feasibility cover makes its
dependent safety result vacuous/unknown. Solver configurations, logs, and VCDs
are retained in the external work directory; the immutable bundle is not
modified. `--formal-jobs` parallelizes independent bundled safety/cover jobs
and independent selected-candidate sites. Stages inside one candidate site
remain ordered; plans, evidence, and report order remain deterministic.
In a joint run, candidate M36/M38 jobs use deterministic subdirectories of that
same external root; an exact in-session M39 reuse reports its retained root when
one exists. Persistent proof-cache hits do not fabricate old workspace paths.

Each verification goal is routed against its own declared clock/reset pair.
Goals in two supported synchronous domains can execute independently; an
unsupported domain skips only goals in that domain. This does not add a
cross-domain temporal property or change the general backend domain boundary.

The formal triggers are deliberately distinct:

- a non-`off` `--formal-policy` without `--verify` runs only the existing M39
  selection-time M36 gate;
- `--verify` with policy `off` runs M35/source safety and covers;
- `--verification-bundle` publishes the base safety/cover bundle and linking
  plan but does not prepare or execute selected-candidate M36/M38;
- `--verify` with a non-`off` policy additionally executes compatible selected-
  candidate Clash M36, direct-SV M36, and advisory M38 evidence.

When a joint compiler run has prepared selected-candidate M36/M38 inputs, bundle
publication stores exact hash-validated replay companions. `zlang-verify`
executes those frozen routes without recompiling source or rerunning M39
selection. A base bundle continues to replay only its M35/source safety and
cover jobs. A raw safety/cover run uses
`zlang-verification-run-report-v7`; a run that produces candidate reports uses
`zlang-compiler-verification-report-v1`, which wraps that raw report and keeps
M36/M38 results separately typed. A safety or executed M36/M38 counterexample
exits `1`; unavailable/unknown/vacuous safety evidence exits `2`. An unavailable
advisory candidate route and an ordinary bounded cover miss do not themselves
fail the command. See
[First-class verification goals and contracts](optimization-formal.md#first-class-verification-goals-and-contracts).

## Source files

A file may contain imports, type aliases, structs, pure functions, operator
overloads, rewrite declarations, target/resource declarations, and one or more
modules:

```zlang
import std.math.complex

type Byte = u8

struct Pair<type T> {
    left  : T
    right : T
}

fn widen(x : Byte) -> u9 { extend<9>(x) }

module Example {
    in  x : Byte
    out y : u9 = widen(x)
}
```

Select a top explicitly when a file contains several modules:

```sh
.venv/bin/zlangc design.zl --top Example --check
```

Names are case-sensitive. There are no semicolons. Line comments use `//`.
Non-nested block comments use `/* ... */`; both forms are lexical whitespace.
Decimal, hexadecimal (`0x`), and binary (`0b`/`0B`) integer literals may contain
valid underscore separators.

## Reading common notation

| Form | Meaning |
| --- | --- |
| `name = expression` | Drive a combinational value or resource control. |
| `state <- expression` | Schedule next-state at the active edge. |
| `source -> destination` | Connect typed endpoints or bind a command. |
| `key => expression` | Select a `switch`/choice arm. |
| `thing @ clk` | Associate a declaration with a clock domain. |
| `field @ 7:4` | Place a CSR field. |
| `object.field` | Select an aggregate, protocol, or resource member. |
| `values[index]` | Read a range-proven vector element; the selector may be runtime. |
| `raw[index]` for `bits<N>` | Read one LSB-zero packed bit; the selector must resolve at compile time. |

ZLang requires exact canonical types at assignment boundaries. It never
silently truncates, extends, performs a numeric signed/unsigned conversion, or
rescales fixed-point values. Equal-width raw boundaries involving `bits<N>` or
flat `vec<N,bit>` are the documented representation-only exception; use
`bitcast<T>` explicitly when the boundary is not already typed.

## Where to continue

- [Types and numerics](types-and-numerics.md)
- [Expressions, functions, and generics](expressions-functions-generics.md)
- [Sequential logic, rules, and storage](sequential-state-storage.md)
- [Hierarchy, protocols, and composition](hierarchy-protocols.md)
- [Optimization and formal verification](optimization-formal.md)
- [Backends, CLI, and tooling](backends-tooling.md)
- [Standard library](stdlib.md)
- [Reproducible projects and dependencies](projects-dependencies.md)
- [Named module interfaces](named-module-interfaces.md)
- [Validated real designs](language-guide.md#validated-real-design-map)
- [Complete language guide index](language-guide.md)
