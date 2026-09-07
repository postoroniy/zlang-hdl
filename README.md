# ZLang HDL

ZLang HDL is an experimental hardware description language and compiler focused on
strong types, explicit cycle semantics, reusable protocols, deterministic
artifacts, and verification-aware implementation selection.

Typed intermediate representations, deterministic identities, structured
diagnostics, manifests, and evidence reports also make compiler workflows
suitable for reviewable automation and software-agent integration. This is an
architectural capability, not a claim about productivity or performance.

> **Alpha software:** the first public release is intended for evaluation and
> real-design feedback. Source syntax, the provisional Python API, and
> non-versioned tooling may change before 1.0. Unsupported combinations fail
> closed rather than publishing guessed RTL.

The initial supported development and release environment is Linux x86-64 with
Python 3.12. ZLang can emit direct SystemVerilog or Clash source; external RTL,
synthesis, and formal tools are optional unless their corresponding flow is
requested.

## Quick start

Clone the repository and install an editable development environment:

```bash
git clone https://github.com/postoroniy/zlang-hdl.git
cd zlang-hdl
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Check a design without emitting RTL:

```bash
.venv/bin/zlang examples/add.zhl --check --verbose
```

Emit direct SystemVerilog:

```bash
mkdir -p build
.venv/bin/zlang examples/add.zhl --systemverilog build/Add.sv --verbose
```

With Verilator installed, a strict lint smoke is:

```bash
verilator --lint-only -Wall build/Add.sv
```

A minimal ZLang module is deliberately small:

```zlang
module Add {
    in a, b : u8
    out y : u9

    y = a + b
}
```

Unsigned and signed addition preserve carry: `u8 + u8` is `u9`, and
`s8 + s8` is `s9`. ZLang does not silently resize, rescale fixed point, cross
clock domains, or adapt protocols.

## What is implemented

The validated language includes:

- exact-width integer, bit-vector, fixed-point, vector, struct, tuple, character,
  and fixed-string types;
- pure functions, generics, compile-time generation, reductions, ROM images, and
  deterministic specialization;
- registers, rules, fixed pipelines, FIFOs, synchronous memories, hierarchy, and
  instance arrays;
- ready/valid, credit, request/response, aggregate protocols, explicit buffering,
  arbitration, and named-domain CDC;
- source-authored RegBus, AHB-Lite, AXI4-Lite, APB, Wishbone, AXI-Stream, CSR,
  math, stream, storage, coding, and target-library components;
- direct-SystemVerilog and Clash emission with source maps and versioned
  BackendArtifact manifests;
- bounded equality saturation, implementation exploration, synthesis evidence,
  M35 safety checks, M36 semantic-reference equivalence, M38 cross-backend
  evidence, M39 formal-aware selection, and source-level verification goals.

The executable [language tour](examples/all_syntax.zhl) is representative, not a
complete support contract. Use the
[current language status](docs/current-language-status.md),
[syntax support matrix](docs/syntax-support-matrix.md), and
[known limitations](docs/known-limitations.md) for the current, bounded surface.

Representative real-design validations include:

- hierarchical DMA and multi-outstanding request/response;
- source-authored AHB-Lite/AXI4-Lite/APB/Wishbone to RegBus/CSR paths;
- fixed-point FIR architectures and target-aware DSP mappings;
- a nine-stage FFT512 SDF reference;
- an attributed, bounded IEEE 802.11a-derived transmitter path covering framing,
  coding, interleaving, mapping, IFFT64, reorder, and cyclic prefix.

These are validation witnesses, not a claim that every possible feature
combination is supported or that measured FPGA timing is guaranteed.

## Backends and verification

| Flow | Status |
| --- | --- |
| Clash | Primary/reference backend for the validated subset |
| Direct SystemVerilog | Supported secondary backend; fail-closed outside its validated subset |
| Verilator | Optional lint and behavioral RTL validation |
| Yosys/SymbiYosys/Z3 | Optional bounded/proven safety and equivalence execution |

Emit Clash and, when Clash is installed, retain generated Verilog:

```bash
.venv/bin/zlang examples/add.zhl -o build/Add.hs
.venv/bin/zlang examples/add.zhl \
  -o build/Add.hs \
  --verilog-dir build/clash-verilog \
  --verilator-lint
```

Create and replay an immutable verification bundle:

```bash
.venv/bin/zlang examples/contracted_add.zhl \
  --top ContractedAdd \
  --verification-bundle build/verify

.venv/bin/zlang-verify build/verify \
  --mode bmc \
  --depth 20 \
  --report build/verification-report.json
```

Bounded model checking is reported as `bounded_pass`, never promoted to
`proven`. Missing tools, bindings, reset semantics, or unsupported routes remain
explicit `unknown`/`skipped` results according to the requested policy.

## Documentation

- [Getting started](docs/getting-started.md)
- [Language guide](docs/language-guide.md)
- [Current language and implementation status](docs/current-language-status.md)
- [Types and numerics](docs/types-and-numerics.md)
- [Expressions, functions, and generics](docs/expressions-functions-generics.md)
- [Sequential logic and storage](docs/sequential-state-storage.md)
- [Hierarchy and protocols](docs/hierarchy-protocols.md)
- [Named module interfaces](docs/named-module-interfaces.md)
- [Standard library](docs/stdlib.md)
- [Optimization and formal verification](docs/optimization-formal.md)
- [Backends and tooling](docs/backends-tooling.md)
- [Projects and dependencies](docs/projects-dependencies.md)
- [Open-source project scope](docs/project-scope.md)
- [Test strategy](docs/testing.md)

The compiler-owned capability registry and release CI are authoritative for
executable support. Documentation should describe semantics and boundaries
without copying mutable pass totals into multiple files.

## Development

Run focused tests serially while debugging. Run the complete suite in parallel:

```bash
.venv/bin/python -m pytest -q path/to/test_file.py
.venv/bin/python -m pytest -n 8 --dist=loadscope -q
.venv/bin/python -m compileall -q zlang tests
git diff --check
```

External-tool tests discover tools from explicit CLI options, documented
environment variables, or `PATH`. Dedicated release jobs require pinned Clash,
Verilator, Yosys, SymbiYosys, yosys-smtbmc, and Z3 versions and reject unexpected
skips.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the DCO, test expectations, and
third-party provenance requirements. Community support is described in
[SUPPORT.md](SUPPORT.md), and vulnerabilities must be reported privately as
described in [SECURITY.md](SECURITY.md).

## License and attribution

ZLang HDL is licensed under the [Apache License 2.0](LICENSE).
Copyright 2026 Viacheslav Vinogradov.

The [2026-09 Community Baseline](docs/licensing/COMMUNITY_BASELINE.md) retains
every capability included in that release, including the language, compiler,
backends, local verification and exploration. Future Enterprise additions start
after the baseline: CSR C/C++ software helper generation and SystemVerilog UVM
helper generation are classified Enterprise, but are not implemented yet.
Existing CSR RTL/JSON/Markdown and Verilator C++ state access remain Community.
See the [editions](docs/editions.md), [project scope](docs/project-scope.md) and
[name and branding policy](TRADEMARKS.md).

The [licensing guide](docs/licensing/README.md) distinguishes compiler licensing,
reference-design provenance and obligations for included generated material.

The 802.11a validation project preserves the MIT attribution for Nirav Dave's
Bluespec reference implementation. The original material and ZLang port and
modifications in that project subtree are distributed under its MIT license.
See the root [NOTICE](NOTICE) and the nested
[Wi-Fi license](examples/projects/80211a_transmitter/LICENSE) and
[notice](examples/projects/80211a_transmitter/NOTICE).

Compiling user-authored ZLang source does not by itself impose Apache-2.0 on the
resulting HDL. Users remain responsible for input code, copied standard-library
material, third-party content, and other material included in generated output.
Generated fixtures committed to this repository remain part of the licensed
repository distribution unless an adjacent notice states otherwise.
