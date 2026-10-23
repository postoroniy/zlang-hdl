<a id="reference-physical-clock-reset-contract"></a>
# Physical clocks and resets


ZLang's `ClockDomain` records the complete physical
contract of one clock and its reset. Concise declarations retain the default
reset contract:

```zlang
clock clk
reset rst @clk
```

normalizes to rising-edge clocking, synchronous active-high reset, native
release, and unspecified power-up state.

<a id="reference-physical-clock-reset-contract-recommended-asynchronous-reset"></a>
## Recommended asynchronous reset

The concise asynchronous form is safe for ordinary state. In a multi-clock
module each annotated domain owns an independent conditioner:

```zlang
clock clk
async reset arst @clk
```

It means asynchronous assertion followed by deassertion synchronized through
two registers. Reset remains active on the first two active clock edges after
the external pin is deasserted; normal state transition resumes on the third
edge. Reassertion at any point immediately restarts that release sequence.

The external pin may be active-low:

```zlang
clock clk
async reset arst_n @clk {
    polarity active_low
}
```

If reset deassertion is already synchronized to this exact clock outside the
generated module, state that physical contract explicitly:

```zlang
clock clk
async reset arst_n @clk {
    polarity active_low
    release externally_synchronized
}
```

This retains asynchronous assertion for resettable state but emits no internal
two-register release conditioner. The annotation is never inferred: every
destination clock domain must receive a release synchronized to that clock.
Omitting `release` retains the safe internal 2FF default.

A register participates in reset only when it declares a reset value:

```zlang
reg control : u8 = 0 // asynchronous reset to zero
reg datapath : u8    // no reset branch or reset sensitivity
```

The unreset register continues to accept enabled clocked updates while reset is
asserted. Its RTL power-up value is unspecified until the design writes it.
Native two-state simulation uses a deterministic zero startup seed, which is
not a source-level power-up or reset guarantee. The opt-in logic-state mode
represents unrefreshed bits as `U`; strict observation rejects `U` or computed
`X` rather than collapsing either to a binary value. See
[Registers and next state](sequential-state-storage.md#reference-sequential-state-storage-registers-and-next-state).

A falling-edge clock uses falling edges for the two release cycles as well.
Polarity affects the external pin and generated RTL; the simulator still
accepts a logical `True` for asserted reset.

The lower-level compatibility spelling remains available for a raw native
asynchronous assertion/deassertion contract:

```zlang
clock clk { edge falling }
reset rst_n @clk {
    mode asynchronous
    polarity active_low
    power_up unspecified
}
```

This full block does not insert synchronized release. When either low-level
physical block is present, every clause in that block is required. Raw
active-low protocol helpers gate transfers with the deasserted high level;
they do not reinterpret the physical pin as active-high.

<a id="reference-physical-clock-reset-contract-backend-lowering"></a>
## Backend lowering

Direct-SV emits a deterministic two-register synchronizer marked with
`ASYNC_REG` for the unannotated concise form. The top-level domain owns it and
routes one conditioned reset through register/rule, FIFO, memory, CSR,
protocol, and child state. A hierarchy does not create one synchronizer per
sibling. A clocked but completely state-free module publishes the same
physical contract without emitting an unused conditioner, because no reset
epoch is consumed.

Within a composed artifact, each child receives the top's conditioned net and
uses it directly as its asynchronous reset. Children and nested grandchildren
never add another release conditioner, so hierarchy depth does not add reset
release latency. The same reusable child compiled as a public top owns its own
conditioner, because that compilation has a new external reset boundary.

`release externally_synchronized` routes the external reset directly into the
asynchronous event control of resettable state and emits no conditioner.
Resetless registers use only the active clock event.

The semantic simulator models each input item as one active edge. It therefore
models immediate assertion at the sampled boundary and the exact two-edge
release hold. Assertion between active edges is additionally checked in RTL
simulation.

<a id="reference-physical-clock-reset-contract-hierarchy-inheritance"></a>
## Hierarchy inheritance

A stateful child that deliberately declares no physical clock/reset may inherit
the enclosing module's sole exact clock domain. This is source ownership, not
an implicit adapter: edge, assertion mode, polarity, release policy/cycles,
power-up policy, and physical names become the child's typed contract and
specialization identity. Reusable stateful RegBus, AHB-Lite, AXI4/AXI4-Lite,
AXI burst, AXI-Stream, APB, and Wishbone cores use this mechanism; source-owned
flat-pin adapters and concrete application/witness tops remain explicit
physical-domain owners.

Inheritance fails closed when the parent has no unique domain. A child that
declares its own domain must still match one exact parent domain; no reset
conversion or CDC is inferred. The Python compilation/session API may supply
one typed `root_domain` when an inherited-domain core itself is selected as a
direct test or tool top. This does not create a source-language default or
bypass validation: the supplied contract participates in the exact compilation
cache key and produced artifacts, while absent or ambiguous ownership remains
an error.

The generated implementation artifact records the contract identity, external
RTL clock/reset paths, edge, assertion mode, polarity,
release policy/cycles, power-up policy, and source origin. Component and
physical-instance records reference that identity.

<a id="reference-physical-clock-reset-contract-formal-applicability"></a>
## Formal applicability

Executable formal work consumes the exact declared contract. With
`power_up unspecified`, the supported combinations are described below.

Formal-only accepted `rule.fire` projections use the effective reset and
polarity, not a raw active-high input assumption. Descendants consume the
conditioned native reset. Rising and falling active edges, active-high and
active-low pins, and synchronous or asynchronous assertion are supported. An
asynchronous reset may use native or exactly two active edges of synchronized
release. Applicable routes are source safety/cover, bindable recursive safety,
same-cycle or fixed-latency II=1 Direct-SV semantic-reference equivalence, and
formal-aware selection. Asynchronous formal execution is limited to one
physical domain.

Multiple supported synchronous domains may still produce independent
goal-local jobs. Asynchronous execution is deliberately single-domain; this
does not define a cross-domain reset relation. Same-cycle pure candidate
equivalence remains reset-independent, while fixed-latency comparison uses the
declared active edge and release window.

The compiler carries the same typed clock/reset contract from the selected
design through RTL bindings, formal jobs and their results. Restoration rejects
missing, corrupted or mismatched contracts rather than guessing a reset mode.

Prepared-artifact, bundle, harness, result-cache, and evidence recipes therefore
separate otherwise identical goals that use different edges, assertion modes,
polarities, or release policies. A non-executable goal still retains its exact
typed contract. If no compatible artifact exists, its physical-domain identity
is absent rather than guessed.

Formal traces distinguish the two reset views. `physical_reset` is the raw
external pin with its declared polarity. `trace:reset` and result
`reset_state` are the normalized active-high *effective* reset used by property
guards, previous-cycle history, feasibility covers, fill masks, and comparison
windows. For synchronized release, effective reset remains asserted for both
release edges even though the external pin is already deasserted. Failure-cycle
and originating-sample attribution therefore describe the real reset epoch,
not merely the raw pin level.

<a id="reference-physical-clock-reset-contract-boundaries"></a>
## Boundaries

One clock domain has exactly one reset. Declaring both `reset` and `async reset`
for that domain is an error. Direct-SV supports multiple independently
conditioned asynchronous domains; current formal execution still skips an
asynchronous goal in a multi-domain module because no cross-domain reset-epoch
relation is claimed. Implicit reset crossings, reset combiners, configurable
synchronizer depth, glitch filters, power-on reset, and macro-selected RTL
semantics are not supported.

`power_up reset` remains typed but fails closed because no common portable
synthesizable initialization mechanism is frozen. DSP48 physical reset pins,
elastic or variable-latency `pipeline(auto)` equivalence, CDC/reset-refinement
proofs, and target BRAM reset pins remain fail closed. So do multi-domain
asynchronous formal execution, general hierarchical semantic-reference equivalence, and every route with missing
or incompatible domain manifests, bindings, assumptions, or observations.
formal-aware selection `available` records unavailable evidence without changing eligibility;
required policies fail unless the existing exact semantic-reference equivalence route executes at the
requested level. No new formal property, observation, or equivalence family is
introduced.
