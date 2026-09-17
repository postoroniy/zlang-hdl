# ZLang HDL

ZLang HDL is an experimental hardware description language and compiler focused on
strong types, explicit cycle semantics, reusable protocols, deterministic
artifacts, and verification-aware implementation selection.

> **Alpha software:** the first public release is intended for evaluation and
> real-design feedback. Source syntax, the provisional Python API, and
> non-versioned tooling may change before 1.0. Unsupported combinations fail
> closed rather than publishing guessed RTL.

The initial supported development and release environment is Linux x86-64 with
CPython `>=3.12,<3.13`. You do not need that interpreter preinstalled: the
recommended `uv` workflow can provision it for the project. Direct
SystemVerilog is the sole supported production RTL backend. External synthesis
and formal tools are optional unless their corresponding flow is requested.

## Quick start

For a release wheel, editor setup, and the optional Verilator/Yosys/SBY/Z3
toolchain, use the
[complete Community language reference](docs/language-reference.md#reference-installing-toolchain).

Clone the repository and let [`uv`](https://docs.astral.sh/uv/) provision the
verified Python runtime and editable development environment:

```bash
git clone https://github.com/postoroniy/zlang-hdl.git
cd zlang-hdl
uv venv --python '>=3.12,<3.13'
uv pip install -e '.[test]'
```

For an ordinary release-wheel installation, install the commands in an isolated
environment:

```bash
uv tool install --python '>=3.12,<3.13' /path/to/zlang_hdl-VERSION-py3-none-any.whl
```

See the complete guide for PATH setup, a pip/venv alternative, and WSL2
instructions.

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
- per-domain registers, rules, FSMs, fixed pipelines, FIFOs, synchronous
  memories, hierarchy, and instance arrays inside single- or multi-clock
  modules;
- ready/valid, credit, request/response, aggregate protocols, explicit buffering,
  arbitration, and mandatory explicit named-domain CDC;
- source-authored RegBus, AHB-Lite, AXI4-Lite, APB, Wishbone, AXI-Stream, CSR,
  math, stream, storage, coding, and target-library components;
- direct-SystemVerilog emission with source maps and versioned BackendArtifact
  manifests;
- bounded equality saturation, implementation exploration, synthesis evidence,
  safety checks, semantic-reference equivalence, formal-aware selection, and
  source-level verification goals.

The executable [language tour](examples/all_syntax.zhl) is representative, not a
complete support contract. Use the
[language support matrix](docs/language-reference.md#reference-syntax-support-matrix)
and [known limitations](docs/language-reference.md#reference-known-limitations)
for the current, bounded surface.
Qwen users can also rely on the tracked
[ZLang HDL project skill](.qwen/skills/zlang-hdl/SKILL.md), which routes work to
the same current guides and executable compiler contracts.

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
| Direct SystemVerilog | Sole supported production backend; fail-closed outside its validated subset |
| Verilator | Optional lint and behavioral RTL validation |
| Yosys/SymbiYosys/Z3 | Optional bounded/proven safety and equivalence execution |

The current verified external-tool configuration is Verilator 5.052, Yosys
0.69, SymbiYosys 0.69, Z3 4.8.12, and Icarus Verilog/VVP 14.0. These are
evidence versions, not compatibility bounds; see the
[installation chapter](docs/language-reference.md#reference-installing-toolchain-verify-the-installation) and
machine-readable [`release/status.json`](release/status.json).

Emit direct SystemVerilog:

```bash
.venv/bin/zlang examples/add.zhl --systemverilog build/Add.sv
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

Try the [runnable formal examples](examples/verification/README.md): prove a
counter invariant, find a rare 64-bit-triggered overflow, check scoped contracts,
and verify ready/valid stalls. The intentionally broken example passes shallow
BMC but fails at a deeper bound, with a source-attributed counterexample.

## Documentation

- [Complete Community language reference](docs/language-reference.md)
- [Concise language quick reference](docs/language-quick-reference.md)
- [Printable PDF reference](docs/ZLang-HDL-Language-Reference.pdf)

The compiler-owned capability registry and release CI are authoritative for
executable support. Documentation should describe semantics and boundaries
without copying mutable pass totals into multiple files.

## Development

Run focused tests serially while debugging. Run the complete suite in parallel:

```bash
.venv/bin/python -m pytest -q path/to/test_file.py
.venv/bin/python -m pytest -n 16 --dist=loadscope -q
.venv/bin/python -m compileall -q zlang tests
git diff --check
```

External-tool tests discover tools from explicit CLI options or `PATH`.
Dedicated release jobs require pinned Verilator, Yosys, SymbiYosys,
yosys-smtbmc, and Z3 versions and reject unexpected skips.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the DCO, test expectations, and
third-party provenance requirements. Community support is described in
[SUPPORT.md](SUPPORT.md), and vulnerabilities must be reported privately as
described in [SECURITY.md](SECURITY.md).

## Support ZLang HDL

If ZLang HDL is useful to you, you can voluntarily support its continued development:

☕ [Buy Me a Coffee](https://buymeacoffee.com/zlanghdl)

## License and attribution

ZLang HDL is licensed under the [Apache License 2.0](LICENSE).
Copyright 2026 Viacheslav Vinogradov.

The Community repository contains the complete compiler, standard library,
local verification flows, examples, tests, and editor integration documented
here. The [Community baseline](docs/licensing/COMMUNITY_BASELINE.md) identifies
the published snapshot and production-backend policy. See also the
[Community edition](docs/editions.md) and [name and branding policy](TRADEMARKS.md).

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
