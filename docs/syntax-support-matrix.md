<a id="reference-syntax-support-matrix"></a>
# Language support matrix


Canonical implementation-selection markers: `implement`, `choice`.

Implementation selection uses the canonical `implement` form for
compiler-discovered candidates and `choice` for user-supplied alternatives. The retired
scalar `pipeline(auto)`, `architecture(auto)`, and `explore` spellings are
migration diagnostics; only protocol `transform pipeline(auto, ...)` remains
source syntax.

`zlang.public_capabilities.CAPABILITY_REGISTRY` is the machine-readable source
for public capability context, semantic status, backend-independent IR,
simulator/Direct-SV/formal coverage, executable witness, and current
limitations. The advertised witnesses are compiled by the public capability
gate. `examples/all_syntax.zhl` is a representative language
tour, not a substitute for that phase-specific capability record.

The following language/backend view is generated from that registry.
`bounded` means the detailed limits in this reference and the capability
record still apply; “supported” is not a promise for every composition.
`Simulation` is the registry's current generic simulator field, not a separate
native-JIT guarantee. Native execution fails explicitly for unsupported plans;
see [Native simulation](native-jit-simulation.md#reference-native-simulation). Formal entries name
the specific eligible relations rather than promising arbitrary proof.

<!-- capability-matrix:start -->
## Language and execution

| Capability | Language | Simulation | Direct-SV |
| --- | --- | --- | --- |
| `scalar-datapath` | supported | supported | supported |
| `exact-literals-and-packed-constants` | supported | supported | supported |
| `fixed-point` | supported | supported | supported |
| `aggregates` | supported | supported | supported |
| `characters-strings-tuples` | bounded | supported | supported |
| `tagged-unions` | bounded | supported | supported |
| `functional-datapath` | supported | supported | supported |
| `compile-time-generation` | supported | not applicable | supported after specialization |
| `concise-exact-lowering` | supported | supported | supported |
| `typed-static-parameters` | bounded | not applicable | supported after specialization |
| `generic-rom-and-table-gather` | bounded | supported | supported |
| `sequential-state` | supported | supported | supported |
| `registered-outputs-and-drive` | supported | supported | supported |
| `physical-clock-reset` | bounded | supported | supported |
| `multi-clock-stateful-logic` | bounded | supported | supported |
| `encoded-enums-and-fsm` | supported | supported | supported |
| `vector-state-update` | bounded | supported | supported |
| `fifo-storage` | supported | supported | supported |
| `writable-memory` | bounded | supported | supported |
| `ready-valid` | supported | supported | supported |
| `credit` | supported | supported | supported |
| `request-response` | supported | supported | supported |
| `aggregate-protocols` | bounded | supported | supported |
| `ahb-lite-stdlib` | bounded | supported | supported |
| `cdc` | bounded | supported | supported |
| `csr` | bounded | supported | supported |
| `contracts` | supported | supported | artifact generation |
| `exploration` | bounded | not applicable | selected candidates |
| `bounded-intent-structural-exploration` | bounded | not applicable | selected typed alternatives |
| `capacity-one-temporal-sharing` | bounded | supported | supported |
| `elastic-ready-valid-pipeline` | bounded | supported | supported |
| `per-bit-native-logic-state` | bounded | supported | unchanged |
| `combinational-instance-arrays` | bounded | supported | supported |
| `runtime-instance-output-projection` | bounded | supported | supported |
| `sequential-instance-arrays` | bounded | supported | supported |
| `storage-instance-arrays` | bounded | supported | supported |
| `ready-valid-instance-arrays` | bounded | supported | supported |
| `request-response-instance-arrays` | bounded | supported | supported |

## Formal coverage

| Capability | Formal coverage |
| --- | --- |
| `scalar-datapath` | same-cycle equivalence where eligible |
| `exact-literals-and-packed-constants` | same-cycle equivalence where eligible |
| `fixed-point` | fixed-point equivalence within supported widths and forms |
| `aggregates` | value equivalence where all required fields are observable |
| `characters-strings-tuples` | scalar and packed-value equivalence where eligible |
| `tagged-unions` | no dedicated tagged-union proof support |
| `functional-datapath` | scalar and fixed-point equivalence where eligible |
| `compile-time-generation` | compile-time only; formal execution not applicable |
| `concise-exact-lowering` | same formal coverage as the equivalent explicit source form |
| `typed-static-parameters` | not a runtime formal target; specialized values participate in the elaborated design being checked |
| `generic-rom-and-table-gather` | storage safety checks only; no dedicated table-gather proof |
| `sequential-state` | register and rule safety checks where the required state and events are observable |
| `registered-outputs-and-drive` | registered outputs inherit state safety checks where observable; transient drive actions have no separate proof route |
| `physical-clock-reset` | reset-aware properties for supported edge, polarity, assertion, and release modes when `power_up` is unspecified |
| `multi-clock-stateful-logic` | source goals are checked in their declared domains; automatic safety checks are bounded |
| `encoded-enums-and-fsm` | no dedicated enum or FSM proof support |
| `vector-state-update` | register safety where the selected element state is observable |
| `fifo-storage` | bounded FIFO safety checks |
| `writable-memory` | no dedicated memory-content proof support |
| `ready-valid` | payload-stability and transfer safety where observable |
| `credit` | sender and receiver credit safety when the exact counter is observable |
| `request-response` | outstanding-transaction and directional-buffer safety where observable |
| `aggregate-protocols` | no general aggregate-protocol proof support |
| `ahb-lite-stdlib` | register-state and ready/valid safety where signals are observable |
| `cdc` | no CDC correctness proof |
| `csr` | CSR state and access safety checks where observable |
| `contracts` | safety checking and bounded cover execution when supported |
| `exploration` | candidate equivalence can gate required selection when a supported equivalence check is available |
| `bounded-intent-structural-exploration` | exact-value alternatives retain equivalence provenance before implementation evidence is evaluated |
| `capacity-one-temporal-sharing` | capacity-one transaction-stream bounded model checking; unbounded proof is not claimed |
| `elastic-ready-valid-pipeline` | ready/valid stability safety only; end-to-end equivalence is not supported |
| `per-bit-native-logic-state` | simulation-only diagnostic semantics; not a formal proof relation |
| `combinational-instance-arrays` | hierarchical equivalence is not supported |
| `runtime-instance-output-projection` | hierarchical equivalence is not supported |
| `sequential-instance-arrays` | bounded safety for exposed child state and events; no general recursive hierarchy proof |
| `storage-instance-arrays` | no formal access to unexposed internal storage contents |
| `ready-valid-instance-arrays` | ready/valid safety where the required signals are observable |
| `request-response-instance-arrays` | request/response safety where the required signals are observable |
<!-- capability-matrix:end -->
