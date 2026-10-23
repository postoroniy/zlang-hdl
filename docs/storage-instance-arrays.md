<a id="reference-storage-instance-arrays"></a>
# Storage-owning instance arrays


This bounded composition slice extends one-dimensional compile-time instance
arrays to same-domain scalar-wire children that own one storage resource.  It
does not add source syntax: the existing `inst lane[N]`, indexed bindings, and
indexed child-output references elaborate into the existing typed hierarchy.

<a id="reference-storage-instance-arrays-supported-boundary"></a>
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
specialization identity. The Direct-SV backend consumes
`ElaboratedInstance` bindings directly and emits one reusable component plus one
application/instance per physical element.

CSR, other protocol/storage mixtures, multiple storage resources, arbitrary
nested storage hierarchy, multiple clock domains, CDC, runtime-selected inputs
or protocol endpoints, and formal observation extensions remain rejected.
Runtime selection of a bit-packable scalar-wire output is supported as a
read-only projection: `lane[select].value` lowers to a typed runtime index over
all statically elaborated `lane[i].value` references. Every child continues to
execute and retain independent state; the selector is only an output mux and
never gates a child or denotes a dynamic physical instance.

The one bounded protocol/storage exception is a storage-only child whose
external ports are exclusively primitive ready/valid and which owns exactly
one globally controlled FIFO.  It uses the
same typed `HierarchicalConnection`, closed component ABI, and FIFO state as a
non-array child.  Ready/valid arrays with synchronous memory or initialized ROM
remain rejected because they do not yet have a frozen protocol/storage contract.

The two-lane synchronous-memory witness checks independent cells and addresses,
write-first same-address collision behavior, registered read output, and reset
clearing both cells and read state. It passes strict Direct-SV/Verilator
behavior.

The [`ZtpuBankedMemory`](../examples/ztpu_banked_memory.zhl) witness elaborates
two four-element arrays into eight independently identified physical memory
children with one shared specialization. Runtime output projection selects the
addressed bank separately for each read port; decoded writes are broadcast to
the matching bank in both replicas. The semantic simulator and Direct-SV/Verilator
agree on masked writes, independent reads, `read_first` collisions,
reset suppression/preservation, and post-reset contents. The versioned
BackendArtifact records every leaf path and no fictitious dynamic instance.
Hidden cells gain
no new formal observation family.
