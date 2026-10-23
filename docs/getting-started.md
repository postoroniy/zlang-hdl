<a id="reference-getting-started"></a>
# Getting started


The representative executable language tour is
[`examples/all_syntax.zhl`](../examples/all_syntax.zhl). It is a practical
starting point rather than an exhaustive list of every legal composition. See
[Known limitations](known-limitations.md#reference-known-limitations) for deliberately unsupported
forms.

Physical ZLang HDL source files use the canonical `.zhl` suffix and MIME type
`text/x-zlang-hdl`. Logical imports remain extension-independent. The former
`.zl` spelling is intentionally rejected so these sources cannot be confused
with the unrelated language that already owns that extension.

<a id="reference-getting-started-install-for-development"></a>
## Install for development

This alpha release requires CPython `>=3.12,<3.13`. The recommended `uv`
workflow does not require a suitable Python interpreter to be installed first;
`uv` can download and manage the verified runtime. From the repository root:

```sh
make venv
source .venv/bin/activate
```

If a compatible interpreter is already available, the complete installation
guide also documents a conventional `venv`/pip path without relying on a
version-specific executable name. Windows users should run the Linux flow
inside WSL2 rather than installing the compiler into native Windows Python.

Checking source and generating SystemVerilog do not require an EDA installation. Verilator, Yosys,
SymbiYosys, `yosys-smtbmc`, Z3 and Icarus are optional external programs for
the corresponding lint, synthesis, formal and simulation flows. See
[Installing ZLang HDL and the external EDA toolchain](installing-toolchain.md#reference-installing-toolchain)
for the recommended OSS CAD Suite route, separate component installation,
release-tested versions and a real formal smoke test.

Check a source file without generating output files:

```sh
zlang examples/all_syntax.zhl --check
```

Without `--top`, `--check` validates every declared module. With `--top NAME`,
it validates only that selected root and does not generate RTL or run formal
checks.

<a id="reference-getting-started-a-first-module"></a>
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

Generate Direct-SV and lint it:

```sh
zlang examples/extended_add.zhl \
  --systemverilog build/ExtendedAdd.sv
verilator --lint-only --top-module ExtendedAdd build/ExtendedAdd.sv
```

An explicit artifact path keeps stdout empty. Use `--verbose` for success
messages on stderr. A bare invocation emits the production Direct-SV
artifact to stdout; use `--check` for semantic validation without emission.

<a id="reference-getting-started-check-named-verification-goals"></a>
## Check named verification goals

Clocked modules can carry same-cycle safety and bounded-reachability goals:

```zlang
assert count_within @ clk { count <= DEPTH }
cover reaches_full @ clk { count == DEPTH }
```

Run the applicable verification jobs and source goals:

```sh
zlang design.zhl --top Top --verify \
  --formal-jobs 4 \
  --verification-report build/verification.txt \
  --verification-work-dir build/verification-work
```

Or publish a hash-validated bundle and replay it without recompiling source:

```sh
zlang design.zhl --top Top \
  --verification-bundle build/verify
zlang verify build/verify --mode bmc --depth 20 \
  --work-dir build/verify-work
```

`bounded_pass` means only that no counterexample was found through the stated
depth. A cover reports `witnessed` or `bounded_unreached`; a cover miss is not a
safety failure. A `prove` request runs BMC first and starts proof only when every
safety job is `bounded_pass`. Solver configurations, logs, and VCDs are retained
in the external work directory; the immutable bundle is not modified.
`--formal-jobs` runs independent jobs in parallel while keeping result order
deterministic.

Each verification goal is routed against its own declared clock/reset pair.
Goals in two supported synchronous domains can execute independently; an
unsupported domain skips only goals in that domain. This does not add a
cross-domain temporal property.

<a id="reference-getting-started-source-files"></a>
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
zlang design.zhl --top Example --check
```

Names are case-sensitive. There are no semicolons. Line comments use `//`.
Non-nested block comments use `/* ... */`; both forms are lexical whitespace.
Decimal, hexadecimal (`0x`), and binary (`0b`/`0B`) integer literals may contain
valid underscore separators.

<a id="reference-getting-started-reading-common-notation"></a>
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
| `raw[index]` for a packed scalar | Read one LSB-zero packed bit; a runtime selector needs a compiler-proven in-range unsigned interval. |
| `raw[offset +: WIDTH]` | Read a fixed-width LSB-based packed window; `WIDTH` is compile-time and the runtime offset must be provably in range. |

ZLang requires exact canonical types at assignment boundaries. It never
silently truncates, extends, performs a numeric signed/unsigned conversion, or
rescales fixed-point values. Equal-width raw boundaries involving `bits<N>` or
flat `vec<N,bit>` are the documented representation-only exception; use
`bitcast<T>` explicitly when the boundary is not already typed.

<a id="reference-getting-started-where-to-continue"></a>
## Where to continue

- [Types and numerics](types-and-numerics.md#reference-types-and-numerics)
- [Expressions, functions, and generics](expressions-functions-generics.md#reference-expressions-functions-generics)
- [Sequential logic, rules, and storage](sequential-state-storage.md#reference-sequential-state-storage)
- [Hierarchy, protocols, and composition](hierarchy-protocols.md#reference-hierarchy-protocols)
- [Formal verification](formal-verification.md#reference-formal-verification)
- [SystemVerilog generation](backends-tooling.md#reference-generate-systemverilog) and
  [CLI](installing-toolchain.md#reference-installing-toolchain-check-and-run-zlang)
- [Standard library](stdlib-overview.md#reference-stdlib)
- [Reproducible projects and dependencies](projects-dependencies.md#reference-projects-dependencies)
- [Named module interfaces](named-module-interfaces.md#reference-named-module-interfaces)
- [Runnable examples](../examples/README.md)
