# Physical clock/reset contract

ZLang's backend-independent `ClockDomain` records the complete physical
contract of one clock and its reset. Legacy declarations remain exact aliases
for the original behavior:

```zlang
clock clk
reset rst @clk
```

normalizes to rising-edge clocking, synchronous active-high reset, native
release, and unspecified power-up state.

## Recommended asynchronous reset

The concise asynchronous form is safe for ordinary single-domain state:

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

## Backend lowering

Direct SystemVerilog emits a deterministic two-register synchronizer marked
with `ASYNC_REG` for the concise form. The top-level domain owns it and routes
one conditioned reset through register/rule, FIFO, memory, CSR, protocol, and
child state. A hierarchy does not create one synchronizer per sibling. A
clocked but completely state-free module publishes the same physical contract
without emitting an unused conditioner, because no reset epoch is consumed.

The semantic simulator models each input item as one active edge. It therefore
models immediate assertion at the sampled boundary and the exact two-edge
release hold. Assertion between active edges is additionally checked in RTL
simulation.

BackendArtifact physical-domain manifest version 10 records the contract
identity, external RTL clock/reset paths, edge, assertion mode, polarity,
release policy/cycles, power-up policy, and source origin. Component and
physical-instance records reference that identity. Default legacy artifacts
retain their previous schema and generated text.

## Formal applicability

Executable formal work now consumes this exact contract instead of assuming a
rising, synchronous, active-high domain. With `power_up unspecified`, the
supported matrix is:

Formal-only accepted `rule.fire` projections use this same effective reset and
polarity, not a raw active-high input assumption. The projection is resolved in
the final formal component scope; descendants consume the conditioned native
reset. The 2026-09-06 correction covers assertion/reassertion, the complete
two-edge release hold, and restart on the third edge in 24 real-RTL profiles.

| Active clock edge | Reset assertion | External polarity | Release | Existing formal routes |
| --- | --- | --- | --- | --- |
| rising or falling | synchronous | active-high or active-low | native | M35/source safety and cover; existing bindable recursive M35; same-cycle and fixed-latency II=1 direct-SV M36; applicable M39 policies |
| rising or falling | asynchronous | active-high or active-low | native | the same bounded routes, for a single physical domain |
| rising or falling | asynchronous | active-high or active-low | synchronized, exactly two active edges | the same bounded routes, for a single physical domain |

Multiple supported synchronous domains may still produce independent
goal-local jobs. Asynchronous execution is deliberately single-domain; this
does not define a cross-domain reset relation. Same-cycle pure candidate
equivalence remains reset-independent, while fixed-latency comparison uses the
declared active edge and release window.

The identity chain is explicit and checked end to end:

- `ClockDomain` contributes clock/reset names, edge, assertion mode, polarity,
  release mode/cycles, and power-up policy to the versioned
  `zlang-physical-domain-contract-v1` digest;
- BackendArtifact v10 publishes the same digest as its physical-domain
  identity together with the external RTL port locators;
- `FormalGoalPlan` schema 2 stores the exact typed contract and, when an
  artifact exists, that physical-domain identity as part of `plan_identity`;
- verification bundle v4 / verification-IR snapshot v3 carries those fields in
  each executable or skipped `VerificationJob`;
- run-report v7 repeats them in every job result, and strict restoration rejects
  plan/job/result disagreement or a corrupted contract digest.

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

## Boundaries

One clock domain has exactly one reset. Declaring both `reset` and `async reset`
for that domain is an error. Multi-domain asynchronous reset, implicit reset
crossings, reset combiners, configurable synchronizer depth, glitch filters,
power-on reset, and macro-selected RTL semantics are not supported.

`power_up reset` remains typed but fails closed because no common portable
synthesizable initialization mechanism is frozen. DSP48 physical reset pins,
elastic or variable-latency `pipeline(auto)` equivalence, CDC/reset-refinement
proofs, and target BRAM reset pins remain fail closed. So do multi-domain
asynchronous reset, general hierarchical M36, and every route with missing
or incompatible domain manifests, bindings, assumptions, or observations.
M39 `available` records unavailable evidence without changing eligibility;
required policies fail unless the existing exact M36 route executes at the
requested level. No new formal property, observation, or equivalence family is
introduced.

## Original-slice validation record

The accepted implementation exercises ordinary registers/rules, FIFO and
ready/valid, memory, CSR, request/response, and nested hierarchy through the
simulator, direct SystemVerilog, and strict Verilator. Tests
cover assertion between edges, two release edges, third-edge restart,
reassertion, both polarities, falling-edge release, exact hierarchy rejection,
manifest round-trip, and the original fail-closed formal/target boundary. The
original combined focused gate reports **217 passed**. Two unchanged-snapshot full regressions report
**2871 passed, 1 documented opt-in skip** in 729.66 s and 974.48 s.
These figures are the acceptance record for the reset slice, not the live
repository baseline; see [Current language status](current-language-status.md)
for the current regression and corpus snapshot.

The superseding formal-applicability focused gates report **53 passed** for
exact plan/job/result identity and bundle round-trip, **65 passed** for adjacent
orchestration/replay, and **26 passed** for physical-manifest/async-reset
coverage. These gates overlap and are therefore not summed. Full-regression
acceptance is intentionally recorded only when the repository-wide run is
complete.
