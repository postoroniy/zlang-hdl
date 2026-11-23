# Public ZLang compiler bugs

Tracking the public Python compiler through **v0.1.0a24** (`c3fd04d8`, 22 November 2026). This includes compiler-generated RTL, native plan construction, Python simulation APIs, verification, and compiler-owned diagnostics. The release ledger also covers HDL-library, editor-package and release-infrastructure issues. [Published a24](https://github.com/postoroniy/zlang-hdl/releases/tag/v0.1.0a24) · [Release ledger](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/release/regressions.json)

| Status | When to use it |
| --- | --- |
| `[found]` | A reproducer on the stated public build demonstrates a difference between expected and observed behavior. |
| `[fixed]` | A public fix and its regression or release record identify the corrected behavior. Record the first fixed build when known. |
| `[not confirmed]` | The available reproducer agrees with the documented behavior, or a specific reproduction attempt gave a different outcome. Keep that evidence. |
| `[ need to check]` | A concrete report or inconclusive result still needs the check named in its row. |

When updating an entry, retain its ID, the affected build, the reproducer, the expected and observed outcomes, and the evidence for the status change. Reopen the same entry if the same failure returns.

## Open and inconclusive

| ID | Status | Finding and next check |
| --- | --- | --- |
| MATH-ARCH-TIMEOUT | `[ need to check]` | The a3 notes record a timeout for a separate architecture equivalence check. The public example records a depth-10 result for `MathExplore`. To resolve the older report, identify the original `MathArchitecture` topology and compare its candidate and proof relation with an a24 result. The guide gives the bounded command for the selected topology. The cause of the historical timeout is open. [Original report](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1200) · [Example and command](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/examples/verification/math-exploration.md#L195) |

## Fixes after a24

| ID | Status | Affected build | Resolution |
| --- | --- | --- | --- |
| MON-INDEX | `[fixed]` | Published `v0.1.0a24` (`c3fd04d8`) | Fixed in public commit `01dab5da`. The Python verification monitor reads the selected vector lane. A true assertion passes and a false one reports the goal, source span and sampled cycle. [Monitor regression](tests/semantic/test_verification_monitor.py) · [Behavior in a24](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/zlang/verification_monitor.py#L144) |
| STRUCT-DUP-FIELD | `[fixed]` | a24 and `01dab5da` | In `P { x=a x=b }`, the second initializer silently replaced the first, even when the first referred to an unknown name. Public source now rejects the second `x` for concrete, context-typed and inferred generic constructors. [Constructor regression](tests/semantic/test_aggregate_ergonomics.py) |
| MON-AGGREGATE | `[fixed]` | a24 and `01dab5da` | Static vector and tuple selections, struct fields and concatenation in assertions raised a sampling error for both true and false predicates. The Python monitor now evaluates them and reports the original assertion location and cycle on failure. [Monitor regressions](tests/semantic/test_verification_monitor.py) |
| IMPORT-DUP-LOCATION | `[fixed]` | a24 and `01dab5da` | A repeated logical import pointed at the first declaration in standalone compilation and could raise a traceback in a project. Public source reports the second declaration during compilation and lock update. Only identical declarations receive an automatic removal edit. [Project regression](tests/integration/test_cli_diagnostics.py) · [Editor fix regression](tests/test_tooling_api.py) |

### Reproduce MON-INDEX

Run this with Python 3.12 against either the published a24 wheel or the corrected source. For `raw=4`, lane 2 is one. Sampling with `expected=1` should succeed. Sampling with `expected=0` should report the failed assertion at cycle 1.

```python
from zlang.compiler import compile_source
from zlang.native_simulation import SimulationError, VerificationAssertionError, VerificationMonitor

source = """module Probe {
    clock clk reset rst
    in raw : bits<4>
    in index : u2
    in expected : bit
    out observed : bit
    lanes : vec<4,bit> = bitcast<vec<4,bit>>(raw)
    observed = lanes[index]
    assert agrees { expected == EXPR }
}"""

for expression in ("raw[2]", "lanes[index]"):
    ir = compile_source(source.replace("EXPR", expression), source_unit="monitor-index.zhl").ir
    monitor = VerificationMonitor(ir.verification_scopes, ir.functions)
    for expected in (1, 0):
        values = {"raw": 4, "index": 2, "expected": expected, "observed": 1}
        try:
            monitor.sample(values, cycle=1)
            print(expression, expected, "pass")
        except (VerificationAssertionError, SimulationError) as error:
            print(expression, expected, type(error).__name__, str(error))
```

| Compiler | Predicate operand | `expected=1` | `expected=0` |
| --- | --- | --- | --- |
| Published a24 and corrected source | `raw[2]` | Sample passes | `VerificationAssertionError` for `$module.agrees`, cycle 1 |
| Published a24 | `lanes[index]` | `SimulationError: … does not support RuntimeIndex` | The same `SimulationError` |
| Corrected source | `lanes[index]` | Sample passes | `VerificationAssertionError` for `$module.agrees`, cycle 1, at `monitor-index.zhl:9:5-9:47` |

## Reported behavior checked against a24

| ID | Status | What the reproducer establishes |
| --- | --- | --- |
| RAM-READ-FIRST | `[not confirmed]` | A combinational observation after a write edge sees the updated word. A register capturing the read on that edge receives the old word, in both standalone and composed designs. ZL-125 separately covers real declaration-order failures. [Capture regression](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_memory_reset_policy.py#L187) |
| QUANTIZE-POLICY | `[not confirmed]` | The typed form `quantize<T>(expr)` requires an explicit policy block. The contextual form has different grammar. ZL-126 corrected which token is highlighted. [Parser regression](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/parser/test_fixed_point_quantize.py#L47) |
| SUM-SCOPE | `[not confirmed]` | In `sum(i in 0..2) x + 1` the addition is outside the unbraced body. With `x=3` the sum is 7. Braces around `x + 1` give 8. [Parser regression](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/parser/test_functional_datapath.py#L48) |
| RESET-AS-DATA | `[not confirmed]` | A declared hardware reset cannot serve as a `when` data predicate. A register initializer specifies its reset value. An ordinary input such as `clear : bit` can control a separate conditional update. ZL-126 corrected the diagnostic for the reset name. [Diagnostic regression](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/lsp/test_semantic_diagnostic_locations.py#L559) |
| ROM-INIT | `[not confirmed]` | The archived generic ROM example supplies a runtime input where a concrete initialization vector is required. The same fixture’s range-proven gather compiles and preserves repeated indices. [Reproducer and checks](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_stdlib_coherence.py#L441) |
| IFFT-FEEDBACK-WIDTH | `[not confirmed]` | The feedback subtraction grows from `fixed<17,15>` to `fixed<18,15>`. Writing it back into the original homogeneous cell requires an explicit conversion. [Typed-width regression](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_80211a_ifft_library_blocker.py#L33) |
| IFFT64-NATIVE-BOUND | `[not confirmed]` | The 64-point source elaborates and round-trips through canonical IR. Native plan construction for this input explicitly reaches the 32,768-node bound. The failure happens before native execution. [Elaboration](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_80211a_ifft_reference.py#L330) · [Native bound](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_80211a_ifft_reference.py#L431) |

## Fixes carried by a24

The following 100 public a24 ledger entries have a Python compiler owner and record a correction. Each ID links to a named regression that ran successfully on the tagged commit in both [CI](https://github.com/postoroniy/zlang-hdl/actions/runs/44969989633) and [Pinned EDA](https://github.com/postoroniy/zlang-hdl/actions/runs/44969989586). Some IDs cover several related symptoms. The [ledger](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/release/regressions.json) lists all selectors for each ID. SIM-001 records the simulation ABI migration and ZL-052 records the enum-backing feature.

### Front end, hierarchy and diagnostics

| Issue and regression | Status | Symptom or corrected behavior |
| --- | --- | --- |
| [ZL-054](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/lsp/test_server.py#L527) | `[fixed]` | Nested declarations could claim the wrong source range in Outline and Sticky Scroll. |
| [ZL-055](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_cli_diagnostics.py#L199) | `[fixed]` | A CLI or project failure could lose its source location, leak a traceback or leave partial output. |
| [ZL-056](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_compile_time_modulo.py#L83) | `[fixed]` | Static packed slice bounds could lose arithmetic grouping or a generate binder. Compile-time remainder by zero must identify the source expression. |
| [ZL-057](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/lsp/test_declaration_diagnostics.py#L92) | `[fixed]` | Declaration, inherited clock and reset errors could point at the wrong source in standalone or imported code. |
| [ZL-059](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_instance_default_inference.py#L25) | `[fixed]` | Derived defaults could trigger premature sibling inference. Compact comparisons and concise typed callable arguments could be parsed as the wrong construct. |
| [ZL-061](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_compile_time_bitwise_conditions.py#L47) | `[fixed]` | Compile-time integer bitwise predicates did not work consistently in masks, conditions and constraints. |
| [ZL-062](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_callable_default_input_types.py#L12) | `[fixed]` | A composite callable input could bind a derived generic default from its caller instead of its declaration. |
| [ZL-063](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_callable_lexical_indices_rtl.py#L10) | `[fixed]` | A callable index or range refinement could capture the caller’s loop binder. |
| [ZL-064](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_guard_refinement_budget.py#L68) | `[fixed]` | Exhausting optional guard refinement could reject a valid opaque call or invent an unproved index bound. |
| [ZL-069](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_cli_diagnostics.py#L71) | `[fixed]` | Malformed callable bodies could escape as parser exceptions. Empty vector literals could leave invalid AST elements. |
| [ZL-070](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_module_nominal_type_bindings.py#L13) | `[fixed]` | Deferred specialization could lose a nominal type by reparsing its display name. |
| [ZL-074](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_hierarchy_observation_correctness.py#L26) | `[fixed]` | Generated immutable locals could use another iteration’s bindings or hide a duplicate declaration. |
| [ZL-080](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_comparison_angle_boundaries.py#L34) | `[fixed]` | A dotted field comparison before a specialized call could be read as a qualified callee. |
| [ZL-082](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_typed_instance_inputs.py#L205) | `[fixed]` | A bare defaulted struct could resolve parameters in the wrong scope. |
| [ZL-084](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_instance_default_inference.py#L102) | `[fixed]` | Child input inference could fail on a later sibling output, a parent local or a statically indexed sibling array. |
| [ZL-126](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/parser/test_fixed_point_quantize.py#L47) | `[fixed]` | Diagnostics could miss the causal token for a missing typed quantization policy, a range binder outside its body or a reset used as data. |
| [LOCK-001](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/project/test_toolchain_lock.py#L72) | `[fixed]` | After a failed project refresh, stale typed products could survive an SDK or stdlib identity mismatch. |
| [LSP-001](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/lsp/test_declaration_diagnostics.py#L205) | `[fixed]` | Diagnostics could land on line one or in the wrong file and survive edits. Mixed register writers must point to the conflicting assignment. |
| [LSP-002](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/lsp/test_generic_default_diagnostics.py#L32) | `[fixed]` | An unspecialized template with derived defaults could produce a false error. Navigation could pick a span from an imported file. |
| [LSP-003](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/lsp/test_generic_body_navigation.py#L53) | `[fixed]` | Definitions in a deferred generic body could go missing or resolve to an unrelated global name. |

### Arithmetic, indices and Direct SystemVerilog

| Issue and regression | Status | Symptom or corrected behavior |
| --- | --- | --- |
| [ZL-058](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_fixed_signed_unsigned_saturation.py#L76) | `[fixed]` | Signed-to-unsigned saturation could discard the source sign before clamping a negative value to zero. |
| [ZL-065](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_deep_expression_lowering.py#L19) | `[fixed]` | Lowering a deep shared expression graph could exhaust the Python call stack. |
| [ZL-066](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/backend/test_registered_vector_write_shape.py#L12) | `[fixed]` | Packed reductions, singleton vectors and indexed output-register writes could select the wrong bits. Index arithmetic must keep its typed width before offset scaling. |
| [ZL-067](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/backend/test_copied_function_regions.py#L32) | `[fixed]` | Copied functional regions could duplicate shared producers or reuse a result across calls with different captures. |
| [ZL-068](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_reserved_gate_identifier.py#L68) | `[fixed]` | Source names such as `config` could become unescaped SystemVerilog keywords on ports or instance bindings. |
| [ZL-078](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/backend/test_typed_instance_inputs.py#L24) | `[fixed]` | A compound child input could be resized by the SystemVerilog instance context, changing its upper bits or sign. |
| [ZL-083](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_comparison_endpoint_rtl.py#L139) | `[fixed]` | Exact endpoint comparisons could trigger strict Verilator constant-range diagnostics on valid RTL. |
| [ZL-115](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_single_bit_vector_projection.py#L31) | `[fixed]` | One-bit packed state could produce an illegal scalar part-select or indexed write. |
| [ZL-116](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_signed_right_shift_semantics.py#L131) | `[fixed]` | Folding or range analysis could narrow a shift distance to the width of the data or treat it as signed. |
| [ZL-117](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_early_generic_simplification.py#L151) | `[fixed]` | A narrow constant AND mask could be applied at the wrong width, changing high result bits. |
| [ZL-118](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_runtime_index_expressions.py#L95) | `[fixed]` | SystemVerilog could evaluate a vector read or write index after widening it, changing which element was addressed. |

### State, storage, protocols and clock domains

| Issue and regression | Status | Symptom or corrected behavior |
| --- | --- | --- |
| [BUS-003](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_public_top_leaf_abi.py#L284) | `[fixed]` | An AXI-Stream endpoint next to a FIFO could be rejected despite having physical ports. |
| [BUS-004](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_public_top_leaf_abi.py#L349) | `[fixed]` | An AXI-Stream port could disappear from backend accounting when the module also had RAM or a registered ROM. |
| [BUS-005](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_backend_correctness.py#L329) | `[fixed]` | Mixing a FIFO with scalar ready/valid controls could lose backpressure or reject a valid connection. |
| [BUS-006](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_public_top_leaf_abi.py#L487) | `[fixed]` | AXI-Stream handshakes or payload fields could be omitted when CSR access was present in the same module. |
| [BUS-007](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_backend_correctness.py#L55) | `[fixed]` | FIFO observations could use a different SystemVerilog spelling from the declaration of a FIFO with a reserved name. |
| [ZL-060](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_storage_control_locals.py#L29) | `[fixed]` | Storage controls could lose elaborated locals. Invalid FIFO or ROM targets with an extra path component could be accepted. |
| [ZL-072](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_fpga_storage.py#L252) | `[fixed]` | A synchronous RAM with `no_change` could replace the held read result on a same-address write. |
| [ZL-075](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_hierarchy_observation_correctness.py#L242) | `[fixed]` | A read-only child protocol observation could read the wrong driver. Reusing a delayed binding could introduce extra state or latency. |
| [ZL-076](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_memory_controller_colocation.py#L83) | `[fixed]` | A globally controlled RAM could not coexist correctly with ordinary registers and rules. |
| [ZL-077](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_memory_controller_colocation.py#L113) | `[fixed]` | Reset could advance later stages of a native RAM read pipeline whose read data should have been held. |
| [ZL-079](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_independent_state_selection.py#L86) | `[fixed]` | Independent conditional action groups could exhaust the selection budget by enumerating their Cartesian product. |
| [ZL-085](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_ported_memories.py#L272) | `[fixed]` | Scheduled RAM could lose the prior read value on a `no_change` collision. A write blocked by reset could appear on a write-first bypass. |
| [ZL-086](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_instance_inference.py#L218) | `[fixed]` | Dependency analysis could report a combinational cycle through a registered child output. |
| [ZL-087](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_memory_reset_policy.py#L127) | `[fixed]` | A named RAM port with preserved read data could start with uninitialized pipeline stages. |
| [ZL-090](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_backend_correctness.py#L89) | `[fixed]` | Direct-SV could drop or conflate a second globally controlled FIFO or RAM in one module. |
| [ZL-091](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_backend_correctness.py#L215) | `[fixed]` | Mixing RAM, ROM and FIFO could omit storage controls or ROM contents in a top or specialized child. |
| [ZL-094](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_memory_reset_policy.py#L240) | `[fixed]` | A generated RAM pipeline stage could share its RTL name with a public signal. |
| [ZL-099](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_ported_memories.py#L472) | `[fixed]` | A named RAM port’s generated write-mask signal could collide with another module signal. |
| [ZL-102](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_ported_memories.py#L189) | `[fixed]` | Dependency analysis could miss a combinational loop through named RAM read or write controls. |
| [ZL-103](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_hierarchical_async_memory_cycles.py#L350) | `[fixed]` | A loop could pass through FIFO lookahead, RAM bypass or a combinational child output without a diagnostic. |
| [ZL-104](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_csr_hierarchy_projection.py#L505) | `[fixed]` | Feedback through a child’s ready/valid payload or CSR readback could escape cycle detection. |
| [ZL-105](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_hierarchical_async_memory_cycles.py#L508) | `[fixed]` | Nested ready/valid links and a full buffer’s ready path could hide feedback. Native push could advance on a stalled transfer. |
| [ZL-106](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_hierarchical_async_memory_cycles.py#L633) | `[fixed]` | A scalar protocol member or request/response payload could hide feedback across a hierarchy boundary. |
| [ZL-107](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_csr_hierarchy_projection.py#L505) | `[fixed]` | Nested static slices of a CSR readback could be assigned the wrong dependency through an immutable local. |
| [ZL-108](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_hierarchical_async_memory_cycles.py#L248) | `[fixed]` | Ready/valid feedback through a live RAM or FIFO control in the enclosing module could escape detection. |
| [ZL-109](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_csr_hierarchy_projection.py#L505) | `[fixed]` | Selecting an independent packed lane across a hierarchy or CSR boundary could falsely close a feedback loop. |
| [ZL-110](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_public_top_leaf_abi.py#L349) | `[fixed]` | A packet-arbitration or VC-credit module combined with storage could reach Direct-SV emission without an implementable binding. |
| [ZL-111](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_csr_hierarchy_projection.py#L505) | `[fixed]` | A constant vector selection could inherit dependencies from unselected elements, rejecting legal storage feedback paths. |
| [ZL-112](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_multi_clock_stateful.py#L139) | `[fixed]` | A foreign clock domain in a nested rule predicate could escape the CDC check. |
| [ZL-113](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_a24_runtime_boundaries.py#L333) | `[fixed]` | Clock provenance could be lost through child bindings. CDC beside ordinary outputs could lose a driver, port or unique synchronizer name. |
| [ZL-114](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_a24_runtime_boundaries.py#L363) | `[fixed]` | A local request/response interface with ambiguous clock ownership could be admitted. |
| [ZL-119](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_unified_state_storage.py#L173) | `[fixed]` | Register guards comparing equal values at different widths could be treated as disjoint, bypassing required priority. |
| [ZL-120](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_hierarchical_async_memory_cycles.py#L18) | `[fixed]` | Zero-extension padding in a RAM address could be mistaken for live feedback. Sign-extension bits do carry a dependency. |
| [ZL-123](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_hierarchical_async_memory_cycles.py#L748) | `[fixed]` | A RAM address selected after `reshape` or a materialized `map` could inherit a dependency from an unselected element. |
| [ZL-125](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_memory_reset_policy.py#L156) | `[fixed]` | Icarus could reject legal RAMs or chained FIFOs when an address, materialized read or handshake used a signal declared later. |

### Python simulation planning, comparison and debugger inputs

| Issue and regression | Status | Symptom or corrected behavior |
| --- | --- | --- |
| [SIM-003](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_comparison_deadlines.py#L49) | `[fixed]` | An RTL build timeout could be reported as a simulation timeout or leave a descendant tool running. |
| [SIM-004](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_resettable_write_controls.py#L51) | `[fixed]` | Native plans could drop source write enables for resettable registers, changing reset or rollback behavior. |
| [SIM-005](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_comparison_stimulus.py#L21) | `[fixed]` | A comparison could misread strided aggregate outputs, resolve a relative work directory twice or generate one executable block per stimulus event. |
| [SIM-006](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_simulation_comparison_union.py#L12) | `[fixed]` | Native/RTL comparison could repack a physical wire or protocol output as the wrong value, including a tagged union. |
| [SIM-007](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_logic_state.py#L383) | `[fixed]` | Initializing or inspecting an `out reg` through its public name could miss its native storage slot. |
| [SIM-008](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_program_diagnostics.py#L26) | `[fixed]` | Formatting a failed simulation program could expand its retained hardware graph and obscure the original error. |
| [SIM-009](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_public_top_leaf_abi.py#L487) | `[fixed]` | Standalone CSR comparison and manifests could select a child-only observation instead of the public access port. |
| [SIM-010](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_native_jit_storage.py#L214) | `[fixed]` | A FIFO readiness or fault observation could ignore its own effective reset during synchronized release. |
| [SIM-011](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_comparison_stimulus.py#L53) | `[fixed]` | A generated comparison testbench’s DUT instance name could collide with a physical signal. |
| [SIM-012](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_comparison_stimulus.py#L90) | `[fixed]` | A generated comparison testbench could have the same module name as a compiled design. |
| [SIM-013](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_logic_state.py#L459) | `[fixed]` | A malformed event could change inputs or reset before validation finished. |
| [SIM-014](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_native_union_padding.py#L36) | `[fixed]` | A native input or event could accept a value of a different nominal tagged-union type. |
| [SIM-015](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_a24_runtime_boundaries.py#L405) | `[fixed]` | A failed direct `set()` could update some protocol fields before rejecting another. |
| [SIM-016](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_a24_runtime_boundaries.py#L468) | `[fixed]` | Advancing a lazy event iterator too early could lose the last valid event when the iterator raised. |
| [SIM-017](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_a24_runtime_boundaries.py#L507) | `[fixed]` | Comparison of request/response union payloads could read the wrong physical ABI leaves. |
| [SIM-018](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_native_jit_cdc.py#L148) | `[fixed]` | Native scalar CDC outputs could remain visible during effective destination reset between clock edges. |
| [SIM-019](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_cdc.py#L67) | `[fixed]` | Native pulse-spacing checks could use stale state across synchronous reset or conditioned release. |
| [SIM-020](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_native_jit.py#L1199) | `[fixed]` | A multi-limb unsigned shift count could lose its high bits in binary or four-state simulation. |
| [VSIX-002](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_native_debug_controls.py#L100) | `[fixed]` | Live debug event metadata could miss a clock front or reset-only event, or misstate edge or reset polarity. |
| [VSIX-003](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_native_debug_controls.py#L56) | `[fixed]` | A breakpoint on a rule or FSM action could stop on a different line from the requested statement. |
| [VSIX-005](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_native_debug_events.py#L56) | `[fixed]` | JSONL debug-event preparation could pack a signed or structured protocol input incorrectly. |

### Formal verification and evidence reuse

| Issue and regression | Status | Symptom or corrected behavior |
| --- | --- | --- |
| [CI-001](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_formal_tool_binding.py#L14) | `[fixed]` | An OSS tool launcher could run a different Z3 executable from the one selected by the compiler. A cached proof must bind the actual executable. |
| [FORMAL-001](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_formal_evidence_integrity.py#L33) | `[fixed]` | A saved or cached result could survive changes to immutable miter inputs or physical bindings. Invalid UTF-8 cache data must trigger a fresh execution. |
| [FORMAL-002](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_formal_evidence_integrity.py#L151) | `[fixed]` | A DSP proof could ignore real high operand or accumulator bits, or accept a stale primitive model. |
| [FORMAL-003](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_formal_work_directory.py#L13) | `[fixed]` | A retained relative work directory could be prefixed twice, breaking proof or cover execution. |
| [FORMAL-004](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_equivalence_preparation.py#L52) | `[fixed]` | Width reduction could fail to reach the actual RTL bindings. Older preparation products needed separate cache identities. |
| [FORMAL-005](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_rtl_arithmetic_cones.py#L155) | `[fixed]` | Unreachable declarations could obstruct proof preparation. A reachable opaque cell must still block proof. |
| [FORMAL-006](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_bounded_subprocess.py#L92) | `[fixed]` | Proof cleanup could leave uncertain descendants or accept missing obligations. Uncertain execution must remain inconclusive. |
| [ZL-071](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_bounded_subprocess.py#L33) | `[fixed]` | A bounded tool invocation could leave descendants alive after a wrapper started a separate session. |
| [ZL-073](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_reference_fixed_conversion.py#L19) | `[fixed]` | An unsized formal reference intermediate could overflow at 32 bits before fixed-point rounding or saturation. |
| [ZL-081](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_fpga_quantizer.py#L106) | `[fixed]` | Proving a bounded connected lane could unnecessarily expand its entire parent word and exceed checker bounds. |
| [ZL-121](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_same_cycle_verification_formal.py#L191) | `[fixed]` | Formal lowering could drop high shift-count bits and report a false proof. Regenerate bundles with v4 or earlier IR snapshots from source before replay. |
| [ZL-122](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_same_cycle_verification_formal.py#L146) | `[fixed]` | A legal narrow selector for a wider vector could crash formal lowering while it built cases for unreachable indices. |
| [ZL-124](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_verification_monitor.py#L305) | `[fixed]` | The Python verification monitor could raise an unsupported-expression error on slices and bitcasts instead of reporting the assertion outcome. |

## Earlier compiler fixes retained in a24

Issue IDs below link to regressions present and passing at a24. The final table cites the public changelog for earlier fixes recorded in prose.

| Historical issue and regression | Status | Symptom or corrected behavior |
| --- | --- | --- |
| [ZL-001](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_vector_state_update.py#L98) | `[fixed]` | Registered aggregate fields could lose a proved unsigned index range during vector reads or writes. |
| [ZL-012](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_signed_right_shift_semantics.py#L177) | `[fixed]` | An arithmetic right shift of a signed value could introduce zeroes instead of extending the sign bit. |
| [ZL-014](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_signed_relational_semantics.py#L321) | `[fixed]` | An ordered comparison could use unsigned ordering for signed operands in emitted RTL or formal predicates. |
| [ZL-015](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_shared_stateful_specialization.py#L175) | `[fixed]` | Unrelated parent-visible functions could give identical stateful children different specialization identities. |
| [ZL-016](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_nested_when_backends.py#L307) | `[fixed]` | Nested conditional effects in an atomic rule could lose the enclosing guard or first-fault ordering. |
| [ZL-017](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/systemverilog/test_composed_rule_dispatch_scaling.py#L709) | `[fixed]` | Standalone and composed rule dispatch could follow different RTL emission paths and lose state effects. |
| [ZL-038](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_state_selection_scaling.py#L419) | `[fixed]` | State selection for a large conditional controller could exhaust compilation bounds. Preserved inputs check bounded emission and cycle-equivalent execution. |
| [ZL-039](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_csr_hardware.py#L91) | `[fixed]` | CSR status and sticky fields could lose the member path of a typed aggregate input. |
| [ZL-040](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_csr.py#L63) | `[fixed]` | Grouped CSR halves could lose their independent write behavior in projection or execution. |
| [ZL-042](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/backend/test_functional_region_emission.py#L139) | `[fixed]` | Selected expressions and aggregate scatter assignments could expand excessively in emitted RTL. |
| [ZL-043](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/backend/test_wide_scatter_emission.py#L55) | `[fixed]` | A wide OR scatter could expand into repeated hardware descriptions rather than bounded chunks. |
| [ZL-045](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_hierarchy_index.py#L246) | `[fixed]` | The way a parent consumed an aggregate child output could change the child’s identity. |
| [ZL-046](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_registered_outputs.py#L182) | `[fixed]` | Flattening two distinct logical fields could silently produce one physical ABI name. |
| [ZL-047](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_instance_arrays.py#L524) | `[fixed]` | Nested generate and compile-time branches could elaborate incorrect instance bindings or escape the shared expansion bound. |
| [ZL-048](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/semantic/test_state_selection_scaling.py#L491) | `[fixed]` | Sibling FSMs could get different specialization identities solely because of their referring module. |
| [ZL-049](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/simulation/test_native_jit_verification.py#L161) | `[fixed]` | Assertions and covers reading an output-owned register could read a different native slot from the hardware expression. |
| [ZL-050](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/tests/integration/test_ahb_lite.py#L302) | `[fixed]` | A reusable clocked bus child could impose its default reset policy despite an unambiguous enclosing contract. |

| First recorded in | Status | Earlier correction |
| --- | --- | --- |
| [a1](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1285) | `[fixed]` | Formal rule-fire observations could use a different reset from the register logic. |
| [a4](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1179) | `[fixed]` | Candidate discovery and evidence could refer to a different implementation from the selected one. |
| [a4](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1182) | `[fixed]` | A catalog-only alternative could enter emitted RTL, and pipeline discovery could ignore explicit positive latency intent. |
| [a5](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1149) | `[fixed]` | Adding a formal observation could invalidate a scheduled datapath’s single output. |
| [a5](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1151) | `[fixed]` | Unsigned DSP inputs needed room for the physical sign bit. The scheduler also had to reject same-stage dependency cycles. |
| [a5](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1153) | `[fixed]` | A generic target could report a constant Fmax estimate without supporting graph data. |
| [a7](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1103) | `[fixed]` | Hierarchy or inferred locals could erase a child output’s clock-domain provenance. |
| [a7](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1110) | `[fixed]` | Reference queries could publish stale or truncated results from the wrong project snapshot. |
| [a8](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1036) | `[fixed]` | A parser diagnostic with a known location could appear on the first line in the editor. |
| [a8](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1040) | `[fixed]` | Generated RAM reset loops needed block-local iterators in SystemVerilog. |
| [a8](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L1065) | `[fixed]` | After packed ABI identities changed, measured DSP selection needed evidence keyed to the actual graph. |
| [a11](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L993) | `[fixed]` | CSR hierarchy emission, typed local child bindings and preserved RAM read-data reset each had incorrect cases. |
| [a11](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L995) | `[fixed]` | A multi-module CLI error could lose its file, line, column or category. |
| [a14](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L926) | `[fixed]` | Editing related open files could cause false dirty-lock or request errors and stale source spans. |
| [a14](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L929) | `[fixed]` | Child protocol member projections and aggregate simulation could disagree between execution paths. |
| [a16](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L862) | `[fixed]` | Instantiating pure functions could duplicate shared subexpressions exponentially. |
| [a16](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L864) | `[fixed]` | An expensive analysis could leave an editor request waiting indefinitely. |
| [a16](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L867) | `[fixed]` | Lowering nested pure functional regions could duplicate bodies and exhaust the plan budget. |
| [a20](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L758) | `[fixed]` | Emitting typed constant subexpressions could redo work for shared expressions in the same DAG. |
| [a23](https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/CHANGELOG.md#L587) | `[fixed]` | Checking a project could blame the top file for an imported error. Lock updates could expose parser tracebacks. |
