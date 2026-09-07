# Storage-owning scalar instance arrays

This bounded composition slice extends one-dimensional compile-time instance
arrays to same-domain scalar-wire children that own one storage resource.  It
does not add source syntax: the existing `inst lane[N]`, indexed bindings, and
indexed child-output references elaborate into the existing typed hierarchy.

## Supported boundary

A child specialization may contain ordinary registers/rules without storage,
or own at most one of:

- a FIFO;
- a synchronous memory; or
- an initialized ROM.

The legacy globally controlled storage profile remains storage-only: it does
not combine that resource with user registers, next-state assignments, or
rules. The separately validated scheduled-FIFO profile does permit ordinary
register/rule state. FIFO actions and register writes are selected atomically by
the same existing `ResolvedTransition`; the array backend does not create a
second scheduler. Unsupported ownership combinations fail during semantic
analysis rather than reaching a backend that could emit only part of the child.

The child must retain the existing storage-array contract: scalar wire ports,
one inherited clock/reset domain, and no nested instances. Multiple scalar
outputs are allowed. Each array element has a distinct physical instance
identity and storage state; all elements share the one deterministic
specialization identity. Both backends consume `ElaboratedInstance` bindings
directly and emit one reusable component plus one application/instance per
physical element.

CSR, other protocol/storage mixtures, multiple storage resources, arbitrary
nested storage hierarchy, multiple clock domains, CDC, runtime-selected inputs
or protocol endpoints, and formal observation extensions remain rejected.
Runtime selection of a bit-packable scalar-wire output is supported as a
read-only projection: `lane[select].value` lowers to a typed runtime index over
all statically elaborated `lane[i].value` references. Every child continues to
execute and retain independent state; the selector is only an output mux and
never gates a child or denotes a dynamic physical instance.

One bounded source-composition witness builds a logical banked 2R1W store from
two flat arrays of globally controlled 1R1W memory leaves. Each leaf still owns
exactly one memory and has only scalar wire ports; the array parent owns no
additional state. One scalar wrapper may instantiate that banked component, so
the generic Clash sequential-child ABI now returns one or multiple scalar outputs
as one coherent bundled result. This is not general nested storage support and
does not permit a storage-owning leaf to contain another instance.

The one bounded protocol/storage exception is a storage-only child whose
external ports are exclusively primitive ready/valid and which owns exactly
one globally controlled FIFO.  It uses the
same typed `HierarchicalConnection`, closed component ABI, and FIFO state as a
non-array child.  Ready/valid arrays with synchronous memory or initialized ROM
remain rejected because they do not yet have a frozen protocol/storage contract.

## Backend evidence

The two-lane FIFO witness checks exact logical vector order (`v[0]` occupies
the most-significant representation region), blocked pushes, simultaneous
accepted pop/push, independent lane hold, reset, and the first post-reset
transfer.  Direct SystemVerilog and real Clash 1.11 RTL produce the same exact
cycle trace under Verilator.

The two-lane synchronous-memory witness checks independent cells and addresses,
write-first same-address collision behavior, registered read output, and reset
clearing both cells and read state.  It passes strict Verilator behavior through
both backends.

The `RvBufferedLaneArray` witness checks two independent ready/valid FIFO
lanes.  It covers independent downstream stalls, payload stability while
stalled, fill/full backpressure, simultaneous accepted pop/push at full,
independent draining, reset while data is buffered, and the first post-reset
transfer.  Semantic simulation, direct SystemVerilog, and real Clash 1.11 RTL
produce the same exact cycle trace.  Each backend emits one reusable lane
specialization and two distinct physical state owners.

The scheduled-state integration witness uses two `ScheduledLane` elements. Each
lane owns one FIFO and an ordinary `last` register; accepted `q.push(data)` and
the matching register write are one rule action group. Reset, independent lane
state, FIFO front values, and held register outputs agree in the semantic
simulator, direct SystemVerilog, and real Clash 1.11/Verilator runs. This witness
does not broaden the legacy global-control profile or add hidden-cell formal
observations.

The initialized-ROM array is supported by semantic/canonical IR, direct
SystemVerilog, and Clash.  It emits one deterministic companion image, one
shared child specialization, and two independent registered read states.  The
real Clash/Verilator witness checks exact logical lane order, independent
addresses, one-cycle read behavior, hold, and reset.

Clash 1.11's existing `romFilePow2` primitive widens the exact address to its
host `Int` index in generated Verilog.  Strict Verilator therefore reports the
known upstream, non-correctness `WIDTHTRUNC` warning, exactly as it does for a
standalone initialized ROM.  The repository's frozen standalone-ROM acceptance
permits only `-Wno-WIDTHTRUNC` for that generated primitive.  The array witness
uses the same single waiver, while a separate regression deliberately runs
strict lint and confirms that this is the only required exception.  No warning
is hidden in production source and the file-backed ROM architecture is
unchanged.

The [`ZtpuBankedMemory`](../examples/ztpu_banked_memory.zhl) witness elaborates
two four-element arrays into eight independently identified physical memory
children with one shared specialization. Runtime output projection selects the
addressed bank separately for each read port; decoded writes are broadcast to
the matching bank in both replicas. The semantic simulator and both RTL
backends agree on masked writes, independent reads, `read_first` collisions,
reset suppression/preservation, and post-reset contents. BackendArtifact v4
records every leaf path and no fictitious dynamic instance. Hidden cells gain
no new formal observation family.
