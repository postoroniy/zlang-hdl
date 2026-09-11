# From a long expression to a checked pipeline

This Community example exercises the compiler's general scalar scheduling and
formal flow. [math_exploration.zhl](math_exploration.zhl) implements an exact
eight-product correlator:

```zlang
y = a0*b0 + a1*b1 + a2*b2 + a3*b3
        + a4*b4 + a5*b5 + a6*b6 + a7*b7
```

Each input is `u16`, each product is `u32`, and the left-associated source sum
has type `u39`: every addition preserves its carry. Mathematically the maximum
is `8 * 65535 * 65535 = 34358689800`, below `2^35`. We retain the original
`u39` result in every implementation; no narrowing, wrap, rounding or saturation
is introduced. All 256 input bits are unrestricted and may change every cycle.
The variants are deliberately kept explicit so their policy differences are
visible; they do not require a compiler-specific function or DSP primitive.

## Three different questions

| Top | What changes? | Internal latency | II |
| --- | --- | ---: | ---: |
| `MathOneCycle` | Original combinational expression | 0 | 1 |
| `MathArchitecture` | Compiler-selected value/resource intent | 0 | 1 |
| `MathExplore` | Compiler-selected value and exact one-cycle schedule | 1 | 1 |

The first two have **no internal pipeline registers**. The timing experiment
places identical launch and capture registers around every kernel; their entire
arithmetic must fit between two consecutive edges. A failed 10 ns timing check
therefore means this measured one-cycle implementation misses 100 MHz. Complexity
alone cannot prove that statement for every FPGA, ASIC or implementation.

The same canonical `implement` form expresses both policies. A bounded
same-cycle/resource choice uses:

```zlang
y = implement {
    a0*b0 + a1*b1 + a2*b2 + a3*b3
        + a4*b4 + a5*b5 + a6*b6 + a7*b7
    intent { latency <= 4 ii == 1 dsp <= 8 minimize lut }
}
```

Requiring positive latency makes fixed-pipeline candidates eligible:

```zlang
y = implement {
    a0*b0 + a1*b1 + a2*b2 + a3*b3
        + a4*b4 + a5*b5 + a6*b6 + a7*b7
    intent { latency >= 1 ii == 1 dsp <= 8 minimize lut }
}
```

The current generic profile selects an exact one-cycle scheduled candidate.
With a supported Xilinx 7-Series target, the same typed DAG may be covered by
DSP48E1 resources before deterministic exact-N partitioning. The search is
bounded, not a global optimum. Estimates remain distinct from synthesis or
routed timing evidence.

## Generate RTL and inspect the decisions

Run from the repository root with ZLang installed in `.venv`:

```sh
.venv/bin/zlang examples/verification/math_exploration.zhl \
  --top MathOneCycle --systemverilog build/math/MathOneCycle.sv
.venv/bin/zlang examples/verification/math_exploration.zhl \
  --top MathArchitecture --systemverilog build/math/MathArchitecture.sv \
  --architecture-report build/math/architecture.txt
.venv/bin/zlang examples/verification/math_exploration.zhl \
  --top MathExplore --systemverilog build/math/MathExplore.sv \
  --exploration-report build/math/explore.txt \
  --evidence-report build/math/evidence.json
```

No solver runs just because ordinary RTL was generated.

## Ask formal whether the optimization preserved the computation

Put `sby`, `yosys`, `yosys-smtbmc`, and `z3` on `PATH`:

```sh
.venv/bin/python -m tools.math_exploration_formal \
  --output build/math/formal --depth 10 --timeout 120
```

Use a new output directory for each execution. This driver runs the existing
compiler `--verify --formal-policy required_bmc` route and retains the immutable
bundle, structured report, cache, RTL, solver logs and counterexamples. It does
not insert a test-only verifier or constrain inputs to selected test vectors.

```text
Original canonical arithmetic  <->  selected direct-SV RTL   M36 / M39 gate
```

Comparison aligns each result with the original input sample at the selected
exact latency (currently one cycle) and masks reset/fill. The source invariant
`y < 34359738368` is an
additional safety goal, not a substitute for arithmetic equivalence.

Yosys prepares the bit-vector transition system; SymbiYosys orchestrates jobs;
`yosys-smtbmc` asks Z3 whether a violating input/history exists. A
`bounded_pass depth=10` covers the modeled prefix for **all legal symbolic
inputs**, not just 100 simulated samples. It is not an unbounded proof and
does not establish FPGA timing.
These checks concern generated RTL before vendor implementation, not formal
equivalence of Vivado's placed/routed netlist.

The driver also demonstrates three important failures/checks:

- Too shallow a comparison window must be `unknown`, never a vacuous pass.
- Flip the generated output's low bit: equivalence must return `failed` with
  a counterexample.
- Bypass the final register but keep the original exact-latency contract:
  equivalence must return `failed`, exposing incorrect sample alignment.

Both mutations live only in separate generated RTL copies. The original source,
reference expression, property, comparison window and immutable bundle remain
unchanged. These are deliberate defects, not known compiler bugs. No new formal
property family or optimizer rewrite is added.

## Measure the 10 ns constraint

With Vivado 2024.2 available:

```sh
.venv/bin/python -m tools.math_exploration_qor \
  --output build/math/qor --vivado /path/to/Vivado/2024.2/bin/vivado \
  --part xc7z030ffg676-1 --period-ns 10 --jobs 2 --timeout 300
```

This is an out-of-context **core** experiment, not a placed board/pinout design.
Each kernel uses the same launch/capture shell, constraints and physical flow.
The shell adds two sampled-cycle delays to all traces: totals are 2/2/6, while
the pipeline decision itself adds 0/0/4. Compare aligned samples, not outputs
at the same wall-clock cycle. II remains one after fill.

Keep `results.json`, source/RTL hashes, XDC, utilization, clock, setup/hold,
route reports and routed checkpoints together. Only fully routed constrained
register-to-register setup/hold paths support core timing acceptance; OOC
boundary-port warnings are not claims about board I/O timing. The reported
`1000 / (10 - WNS)` is an Fmax **proxy**, not a multi-frequency implementation
sweep. A tool error, timeout or missing report is not a timing success.
The runner exits zero when all three measurements completed, not when every
variant met timing; inspect each row's `setup_pass`, `hold_pass`, and
`timing_pass`. A failing baseline is an expected result of this experiment.

### Historical recorded result — 2026-09-08

Vivado 2024.2 build 5239630, `xc7z030ffg676-1`, 10.000 ns, direct-SV,
identical OOC launch/capture shells, two threads per process, at most two
concurrent jobs:

| Kernel | Internal stages | LUT | Fabric FF | DSP | Setup slack ns | Hold slack ns | Fmax proxy MHz |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Original chain | 0 | 0 | 291 | 8 | -5.372 | +0.801 | 65.05 |
| `architecture(auto)` | 0 | 100 | 291 | 8 | +1.283 | +1.127 | 114.72 |
| `explore` pipeline | 4 | 100 | 394 | 8 | +5.927 | +0.160 | 245.52 |

All use zero BRAM. Fabric FF counts exclude the register sites inside DSPs.
Routed delay proxies are 15.372 / 8.717 / 4.073 ns; all register endpoints are
clocked/constrained, and all three routes have zero routing errors.
The three runs took 63.668 / 55.011 / 52.850 seconds under the stated concurrent
load; these are not isolated compiler-speed measurements.

The original chain **fails** this one-cycle 100 MHz budget. Topology selection
alone already passes it; pipelining is not necessary just to cross 100 MHz here,
but adds substantial timing margin at four extra cycles and 103 fabric FFs.
This is the useful trade-off, not a claim that pipelining is always preferable.

The original OOC all-path report includes a -0.368 ns hold path from an
unlocated input port to a launch register. It is retained as boundary evidence,
not hidden with a false path. The table instead uses constrained register-only
setup **and** hold queries on the unchanged routed checkpoints. Package pins,
board I/O delays and full-design clock integration still need their own closure.
`results.registers.json` preserves this audit separately from the original
all-path `results.json`; future fresh runs query register-only paths directly.

Exact source SHA256:
`db002f4b7aaa492e673d9210f55eb060c5355045f849ff1be613845261534c14`.
The same source snapshot produced the numerical, formal and timing evidence.
This table records the pre-retirement dual-backend experiment. Current
production verification executes only the direct-SV M36/M39 row; the Clash and
M38 rows are historical evidence.

| Check | Recorded result |
| --- | --- |
| `MathExplore`: canonical reference ↔ Clash | `bounded_pass depth=10` |
| `MathExplore`: canonical reference ↔ direct-SV | `bounded_pass depth=10` |
| `MathExplore`: Clash ↔ direct-SV | `bounded_pass depth=10` |
| Too-shallow pipeline comparison | depth 7 `unknown`, minimum meaningful depth 8 |
| Output-bit-flip mutation | `failed`, counterexample at formal cycle 8 |
| Missing-final-stage mutation | `failed`, counterexample at formal cycle 8 |
| `MathArchitecture`: separate topology M39 check | `unknown` after 45 s and 120 s timeouts |

The two mutation results demonstrate that checking arithmetic alone is insufficient:
the formal comparison must preserve the originating sample and pipeline latency.
No bounded result above is labeled `proven`.

The separate `MathArchitecture` rank-1 `folded_p4` equivalence problem timed out
while checking its first symbolic step. It passes the numerical/RTL tests and
the timing experiment, but **does not have a successful formal equivalence
result in this record**. The faster `MathExplore` proof is for its own selected
candidate and must not be transferred to another topology. Required M39 policy
correctly stops on the inconclusive result rather than accepting it or silently
trying a different implementation:

```sh
.venv/bin/zlang examples/verification/math_exploration.zhl \
  --top MathArchitecture --formal-policy required_bmc \
  --formal-depth 4 --formal-timeout 120 \
  --systemverilog build/math/architecture-checked.sv
```

Recorded outcome: nonzero exit and explicit timeout/`unknown`; no accepted
checked artifact. Solver runtime depends on the representation as well as the
mathematics. Lowering the bound does not cure this first-step timeout, and
changing arithmetic semantics to make the proof easier is not acceptable.

## Regression

```sh
.venv/bin/python -m pytest -q tests/integration/test_math_exploration_example.py \
  tests/semantic/test_math_exploration_formal_tool.py tests/test_math_exploration_qor.py
```

The consolidated witness checks exact boundary/random samples, continuous
traffic, fill and mid-stream reset against an independent integer oracle in
the semantic simulator and production direct-SV under strict Verilator. Canonical
round-trip, artifacts and unchanged production RTL after erasing verification
declarations are checked separately from solver and timing evidence.
