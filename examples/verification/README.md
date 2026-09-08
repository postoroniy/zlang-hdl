# Formal verification you can run

For arithmetic optimization rather than state safety, see
[From a long expression to a checked pipeline](math-exploration.md): exact
eight-product math, `architecture`/`explore`, latency-aware Z3 equivalence,
deliberate RTL mutations, and a separate routed 100 MHz experiment.

These four small designs demonstrate the existing ZLang HDL verification flow.
The counterexample example is **deliberately broken**; it is not a known bug in
the compiler, Wi-Fi transmitter, or other example.

Run commands from the repository root after installing ZLang in `.venv`.
Put `yosys`, `sby`, `yosys-smtbmc`, and `z3` on `PATH`. These are real external
tool runs, not mocked tests. The examples use the connected direct-SystemVerilog
formal route; Clash is not required for these commands.

`--check` only checks syntax and types; it does not run Z3. Verification
declarations do not add gates or alter production RTL. `--verify` explicitly
checks the modeled RTL against those declarations and applicable automatic
properties; passing compilation alone is not a formal result.

| Example | Question | Recorded result |
| --- | --- | --- |
| [BoundedCounter](bounded_counter.zhl) | Can any input history overflow a saturating counter? | `proven`; full-capacity cover `witnessed` |
| [RareOverflowBug](rare_overflow_bug.zhl) | Can a rare input bypass the capacity check? | Depth 8 `bounded_pass`; depth 16 `failed` with VCD |
| [ScopedSum](scoped_sum.zhl) | Does a 33-bit sum retain carry, and obey a restricted-input budget? | Global assertions and scoped guarantee `proven` |
| [VerifiedRvBuffer](rv_buffer.zhl) | Does the output remain stable during arbitrary backpressure? | `bounded_pass depth=16`; stall and transfer covers `witnessed` |

## 1. Prove an invariant, not just a test sequence

```sh
.venv/bin/zlang examples/verification/bounded_counter.zhl \
  --verify --verify-require proven --formal-depth 16 --formal-timeout 45 \
  --verification-bundle build/verify/counter \
  --verification-work-dir build/verify-work/counter \
  --verification-report build/verify/counter-result.txt
```

The design contains only a four-bit counter and two unrestricted inputs:

```zlang
assert capacity { count <= 9 }
cover reaches_capacity { count == 9 }
cover exceeds_capacity { count > 9 }
```

This is not the trivial statement that a `u4` fits in four bits: values 10–15
are representable but must not be reachable. The checker considers all legal
`clear`/`increment` histories under its reset model. With `proven`, BMC runs
first, then the induction/prove stage establishes the invariant beyond the
bounded prefix. There is no manually written stimulus sequence.

Expected exit: **0**. `capacity` is `proven`. `reaches_capacity` is `witnessed`;
`exceeds_capacity` is `bounded_unreached`. The latter status alone says nothing
beyond the chosen bound; the separate safety proof establishes the invariant.

Automatically generated register/reset and rule-scheduling jobs also appear.
They are separate obligations, not substitutes for the user-written invariant.

## 2. Find a rare, sequential bug — and see why depth matters

`RareOverflowBug` intentionally permits an increment when the counter is full
when the input matches `0xC0DE_F00D_1234_5678`. For independent uniformly random 64-bit keys,
that key has probability `1 / 2^64` per sample. Directed simulation could find
it if the tester already knew the trigger. Formal does not need that stimulus:
the key is symbolic, and the solver derives a sequence that violates `capacity`.
This is a demonstration of constraint solving, not a cryptographic claim.

Publish the immutable inputs without executing a solver:

```sh
.venv/bin/zlang examples/verification/rare_overflow_bug.zhl \
  --verification-bundle build/verify/rare
```

First use a deliberately insufficient depth:

```sh
.venv/bin/zlang-verify build/verify/rare \
  --mode bmc --depth 8 --timeout 45 \
  --work-dir build/verify-work/rare-shallow \
  --report build/verify/rare-shallow.txt
```

Expected exit: **0**, with `bounded_pass`, **not** `proven`.

Now replay the **same bundle**, without recompiling the source:

```sh
.venv/bin/zlang-verify build/verify/rare \
  --mode bmc --depth 16 --timeout 45 \
  --work-dir build/verify-work/rare-deep \
  --report build/verify/rare-deep.txt
```

Expected exit: **1**. This is success for the demonstration: formal found the
injected bug. Do not hide this exit status in CI; a test for this example must
explicitly expect it. To compile and check in one command instead, use:

```sh
.venv/bin/zlang examples/verification/rare_overflow_bug.zhl \
  --verify --formal-depth 16 --formal-timeout 45 \
  --verification-work-dir build/verify-work/rare-cli \
  --verification-report build/verify/rare-cli.txt
```

The report identifies the assertion's source span and `register:count=0b1010`
(10). The retained `trace.vcd` shows reset, increments and the magic key at the
overflow-causing edge. The key need not retain that value in the final violating
frame. The concise decoded report includes goal-bound observations; inspect
the VCD for other input/history values:

```sh
find build/verify-work/rare-deep -name trace.vcd
# Open a returned path in your waveform viewer, for example GTKWave.
```

Trace cycles include the harness reset/history prefix, so they are not simply
the number of accepted increments. On the recorded run the failure is reported
at cycle 13; do not hard-code that cycle as a public language contract.

The repair is to remove the key bypass. [BoundedCounter](bounded_counter.zhl)
shows the corrected guard and its unbounded invariant proof.

## 3. Keep assumptions local and check they are feasible

```sh
.venv/bin/zlang examples/verification/scoped_sum.zhl \
  --verify --verify-require proven --formal-depth 16 --formal-timeout 45 \
  --verification-work-dir build/verify-work/sum \
  --verification-report build/verify/sum-result.txt
```

`a` and `b` are 32-bit inputs, so there are `2^64` possible operand pairs. The
global assertions check carry preservation and commutativity without limiting
these inputs. The separate contract says:

```zlang
contract bounded_request {
    require legal_operands { (a <= 100) & (b <= 100) }
    ensure budget { total <= 200 }
    cover exact_budget { total == 200 }
}
```

`require` is a verification assumption, **not hardware validation or clamping**.
It applies only to this contract. A real environment must satisfy it if it
relies on the contract guarantee. ZLang additionally asks for a feasibility
witness so an impossible environment cannot silently produce a useful-looking
proof. Global assertions do not inherit `legal_operands`.

Expected exit: **0**; both global assertions and `budget` are `proven`.
The feasibility cover and `exact_budget` are `witnessed`. These results do not
imply that an arbitrary downstream consumer can accept every value of `total`.

## 4. Check backpressure without writing a protocol testbench

```sh
.venv/bin/zlang examples/verification/rv_buffer.zhl \
  --verify --formal-depth 16 --formal-timeout 45 \
  --verification-work-dir build/verify-work/rv \
  --verification-report build/verify/rv-result.txt
```

The payload, input validity and downstream readiness are symbolic. The compiler
derives the existing ready/valid input-stability assumption and output-stability
assertion. Under a stall, the producer must hold `valid` and payload; this is a
protocol assumption, not a restriction that the downstream is always ready.
The two user covers demonstrate an output transfer and a backpressured output.

Expected exit: **0**, with `bounded_pass depth=16` and witnessed covers.
This buffered connection currently publishes RV stability, not a separate set
of FIFO accounting observations. It does **not** establish packet conservation,
ordering, or eventual delivery.

For this same example, an additional `--verify-require proven` attempt on the
recorded toolchain returns **2 / `unknown`**: the induction stage does not close
although its BMC stage passes. An induction countertrace is not necessarily
reachable after reset and is not reported as a DUT counterexample. Do not turn
this result into either `proven` or a claimed functional defect. Improving that
proof route is separate from these examples.

## What the results mean

ZLang builds typed properties and connects them to emitted RTL. Yosys lowers the
design; SymbiYosys orchestrates the jobs; `yosys-smtbmc` uses Z3 for these runs.
Formal checks the modeled design and stated properties/assumptions, not every
unstated requirement, analog behavior, or arbitrary physical implementation.

| Result | Meaning |
| --- | --- |
| `bounded_pass depth=N` | No counterexample within the modeled bound |
| `proven` | The requested safety proof completed, subject to the model/assumptions |
| `failed` | A reachable counterexample was produced |
| `witnessed` | One trace reaches the cover condition; not an eventuality guarantee |
| `bounded_unreached` | No cover witness within the bound; not a proof of unreachability |
| `unknown` / `skipped` | Incomplete evidence or an unavailable route/tool, never success |

Normal text reports show statuses and source spans. Add
`--verification-format json` to `zlang`, or `--format json` to `zlang-verify`,
for machine-readable output. A prove run retains its earlier `bounded_results`.
Raw logs and VCDs stay in the work directory, outside the immutable bundle.

Recorded 2026-09-08 with Yosys/SBY 0.68 and Z3 4.8.12. The tests require actual
solver execution and check status, assumption scope, retained traces and replay;
they do not accept tool absence as success. See
[the formal guide](../../docs/optimization-formal.md) for reset assumptions,
backend routing, M36/M38/M39 and current applicability boundaries.
