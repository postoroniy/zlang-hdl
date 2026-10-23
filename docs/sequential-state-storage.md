<a id="reference-sequential-state-storage"></a>
# Sequential state, rules, pipelines, and storage


ZLang keeps combinational values, next-state actions, timing, storage, and
physical clock ownership explicit in source. Synchronous active-high reset is
the default. Each independently declared clock has one reset contract and may
own ordinary state in the same module.

<a id="reference-sequential-state-storage-clock-and-reset"></a>
## Clock and reset

Single-domain modules declare one pair:

```zlang
clock clk
reset rst
```

Because newlines are ordinary whitespace, the compact adjacent spelling is
equivalent and introduces no combined declaration or inferred domain:

```zlang
clock clk reset rst
```

Clock and reset are declared together. Multiple domains require explicit reset,
port, and state ownership wherever inference would be ambiguous:

```zlang
clock source_clock
reset source_reset @source_clock

clock destination_clock
reset destination_reset @destination_clock

reg source_count : u32 @source_clock = 0
reg destination_count : u32 @destination_clock = 0

rule TickSource @source_clock when 1 {
    source_count <- truncate<32>(source_count + 1)
}

rule TickDestination @destination_clock when 1 {
    destination_count <- truncate<32>(destination_count + 2)
}
```

An unannotated stateful declaration remains concise in a single-clock module.
In a multi-clock module its domain may be inferred from one unique destination
or dynamic operand domain; otherwise analysis reports `ZL-DOMAIN-AMBIGUOUS`.
Constants are domain-neutral. Combining dynamic values from different domains,
including through an unannotated local, reports `ZL-DOMAIN-CROSSING`.

Rules and priorities are scheduled within a domain. Rules in unrelated domains
do not acquire cross-clock mutual exclusion or atomicity. FSM-generated state
and rules inherit the FSM annotation. Exact `pipeline(N) @clk { ... }`, FIFO,
memory, ROM, and CSR state similarly retain one resolved owner; inserted
pipeline and balancing registers never act as CDC.

Implicit clock-domain crossing is always rejected. See
[Hierarchy and protocols](hierarchy-protocols.md#reference-hierarchy-protocols-clock-domain-crossings).

The recommended asynchronous spelling asserts immediately and releases only
after two active clock edges:

```zlang
clock clk
async reset arst_n @clk { polarity active_low }
```

Normal state transition resumes on the third edge after external deassertion.
Use the externally synchronized release contract only when the external source
already guarantees synchronous deassertion to this exact clock. It removes the
internal 2FF while retaining asynchronous reset for state with a declared reset
value.
The low-level `reset ... { mode asynchronous ... }` spelling retains raw native
deassertion for compatibility. Exact polarity, falling-edge behavior,
hierarchical propagation, and unsupported formal/target routes are documented
in the [physical clock/reset contract](physical-clock-reset-contract.md#reference-physical-clock-reset-contract).

<a id="reference-sequential-state-storage-registers-and-next-state"></a>
## Registers and next state

```zlang
reg count : u8 = 0
count <- truncate<8>(count + 1)
```

The explicit multi-clock spelling places the annotation before the initializer:

```zlang
reg count : u8 @datapath_clk = 0
```

Expressions read committed beginning-of-cycle state. All accepted `<-` updates
become visible together at the active edge. A register without an accepted
update holds. Reset restores the declared constant. Multiple unordered writers
are rejected.

The reset value is optional. `reg retained : u8` has no generated reset branch
or reset sensitivity; its hardware value is unspecified until written. Native
two-state simulation starts it from a deterministic zero seed for tool parity,
which is not a power-up or reset guarantee in generated RTL.

Use `zlang sim --logic-state` to opt into the per-bit simulation lattice
`0`/`1`/`U`/`X`. State without an initializer begins `U` (never refreshed).
An accepted state write converts unresolved `U` bits to `X` (refreshed but
unknown), while a non-write holds `U`. Exact controlling values still resolve:
`U | 1` and `X | 1` are `1`, while `U & 0` and `X & 0` are `0`.
`--strict-uninitialized` implies logic-state execution and turns a typed read
of either `U` or `X` into an error that identifies which state was observed.

Observations belong to the selected implementation. Exact binary optimization
need not preserve the original expression's U/X propagation, and strict mode
does not diagnose every cancelled operand in the source. U/X tags are not an
algebraic hardware domain; unknown stateful guards or memory addresses still
fail rather than choosing a deterministic branch or location.

Python callers may use `set_logic`, `get_logic`, `outputs_logic`, and
`Program.create(initial_registers=...)`. Packed spellings are MSB-first,
case-insensitive `0`, `1`, `u`, or `x`; a single character broadcasts to the
full width. Register names use the design's state paths, including explicit
child paths such as `child.counter`; ambiguous or
non-register names fail before any state is changed. An edge that fails because
of an unknown stateful control or native execution error rolls back logic state,
event flags, and trace state atomically. CLI equivalents are `--set-logic` and
`--initial-reg`. Standard FST/VCD represent both `U` and `X` as `x`; ZLang
emits a same-width hidden companion `__zlang_meta/*_u_mask` whose set bits
distinguish `U`. `--trace-signal` may be repeated to select the traced surface,
and the initial sample is emitted at time zero. The waveform is an event-index
visualization: each JSONL event occupies two event-domain trace ticks, reset changes
precede that event's selected clock edges,
and falling-edge, active-low, and coincident-domain levels follow the compiled
domain contract. It does not reconstruct analog time or simulator delta-cycle
regions that are absent from the event schedule.

This is not full Verilog simulation: high impedance (`Z`), electrical
resolution/strength, and Verilog event-region semantics are not modeled.
Ordinary simulation remains deterministic two-state simulation, and generated
Direct-SV is unchanged by these simulator options.

An ordinary output is a combinational wire. Output-visible state is declared
explicitly with `out reg`:

```zlang
out remaining : s32              // combinational wire
out reg accepted : s32 = 0       // resettable stored output
out reg retained : u8 @data_clk  // stored output with no reset branch
```

A registered output is one state resource and the public output port at the
same time. It is readable inside the module, accepts `<-` updates, and holds
when no accepted action updates it. Its optional initializer is its reset
value; omitting it has the same unspecified-hardware-reset semantics as an
ordinary register without an initializer. The type must be register-storable.
Ready/valid, credit, packet, request/response and other protocol endpoints are
not `out reg` ports; their individual protocol fields retain their existing
ownership and flow-control rules.

ZLang `out reg` names actual architectural storage. It is not the historical
Verilog/SystemVerilog `output reg` declaration category: a ZLang registered
output has hold behavior, one explicit state owner, and the same clock/reset
and atomic-update rules as an ordinary ZLang register.

Control registers may use nominal enums. Reset and next-state values must be
members of the exact same declaration, and an exhaustive enum `switch` is the
preferred finite-state-machine spelling:

```zlang
enum Phase { Idle Active Done }
reg phase : Phase = Phase.Idle
phase <- switch phase {
    Phase.Idle => start ? Phase.Active : Phase.Idle
    Phase.Active => finish ? Phase.Done : Phase.Active
    Phase.Done => Phase.Idle
}
```

The semantic simulator and Direct-SV share the declaration-order
ordinal encoding. The source-authored APB bridge in
[`stdlib/bus/apb.zhl`](../stdlib/bus/apb.zhl) uses an enum for its internal phase
without exposing it through the external bus ABI.

For control-oriented state, `fsm` is concise syntax for the same enum register
and guarded atomic rules:

```zlang
enum TxPhase { Idle Header Payload Done }

out reg remaining : u32 = 0

fsm phase = TxPhase.Idle {
    Idle {
        when start -> Header { remaining <- length }
    }
    Header { when header_sent -> Payload {} }
    Payload {
        priority {
            when abort -> Idle {}
            when last -> Done {}
        }
    }
    Done { -> Idle {} }
}
```

The qualified initial member in `fsm phase = TxPhase.Idle` infers the exact
nominal enum type. The explicit compatibility spelling
`fsm phase : TxPhase = Idle` remains accepted; neither form infers from guards,
actions, or transition targets.

Every member appears exactly once. A state body is `hold`, one transition, or
an explicit `priority` block; source order never creates hidden priority. The
state update and transition actions form one ordinary atomic rule, reset
restores the declared initial member, and direct writes to the generated state
register are rejected. `fsm` is lowered before canonical/backend IR and does
not add a second scheduler or runtime procedural `if`.

The production Wi-Fi `IeeeIFFTInputStrip` is a second source-authored witness:
its `Data`/`Flush` FSM atomically starts and retires a 64-transfer zero-frame
flush, holds under backpressure, and returns to `Data` after the last accepted
sample. Mid-flush reset restores the declared initial state and discards the
incomplete protocol epoch.

<a id="reference-sequential-state-storage-runtime-indexed-vector-register-updates"></a>
### Runtime-indexed vector register updates

A one-dimensional vector register supports one range-proven runtime element
write in an action group:

```zlang
reg samples : vec<64,u8> = generate(i in 0..64) 0

when capture {
    samples[index] <- value
}
```

This is exactly `samples <- VectorUpdate(samples,index,value)` over the
beginning-of-cycle snapshot. The unsigned index must be statically proven in
range. A second dynamic or static write to the same vector register conflicts;
nested paths, slices, runtime-selected instances, and automatic memory
inference remain outside this bounded form.

A rule guard may establish the required bound through a simple unsigned
comparison and conjunction:

```zlang
when (index < 64) & capture {
    samples[index] <- value
}
```

That fact is scoped to this rule only. The bounded refinement understands these
simple comparisons/conjunctions; `index <= 64`, disjunctions, and uses outside
the guarded rule do not prove the required `0..63` range.

<a id="reference-sequential-state-storage-delays-and-fixed-pipelines"></a>
## Delays and fixed pipelines

```zlang
delayed = delay<2>(value)
piped = pipeline(3) { expression }
```

`delay<N>` adds exactly `N` zero-reset scalar stages. `pipeline(N)` is an exact
visible-latency contract: for a supported pure scalar expression DAG, planning
may place real computation boundaries within those `N` cycles and inserts the
required reconvergent-path balancing. It never changes the requested latency or
II, and `pipeline(1)` remains exactly one cycle. Unsupported nodes fail closed;
the emitter does not fall back to an unreported whole-expression output delay.
Outside a scheduled pipeline region, non-timeless operands at an operator must
still be latency-aligned explicitly.

Compiler-selected latency/resource alternatives use
`implement { expression intent { ... } }`. Scalar `pipeline(auto)` is retired;
protocol `transform pipeline(auto, ...)` is a separate ready/valid construct.

<a id="reference-sequential-state-storage-exact-module-timing-contracts"></a>
## Exact module timing contracts

A scalar datapath module may publish one exact contract shared by all of its
wire outputs:

```zlang
module TimedChild {
    clock clk
    reset rst
    in x : u8
    out y : u8

    timing {
        latency 4
        ii 1
    }

    y = pipeline(4) { x }
}
```

The contract describes observable behavior; it does not itself insert registers
or authorize retiming. Constants are timeless, ordinary input-dependent logic
is known at latency zero, and explicit `delay<N>`/`pipeline(N)` adds exactly
`N`. A supported pipeline region may use its selected schedule to
place and balance those internal cuts. Timeless values may join a known path;
joins outside that graph still require equal known latency.

Registers, rules, FIFO/memory/ROM observations, protocols, and uncontracted
child outputs are `unknown` under the current bounded contract. A positive latency
requires exactly one clock/reset domain. Only `ii 1` is accepted. A contracted
child adds its declared latency to the common known latency of its bound scalar
inputs; an unaligned parent join is rejected instead of silently balanced.

This is distinct from `implement` constraints, target estimates, and measured
evidence. A selected profile must preserve an exact module contract as
immutable public behavior.

<a id="reference-sequential-state-storage-guarded-atomic-actions"></a>
## Guarded atomic actions

The concise action form is:

```zlang
when increment {
    count <- truncate<8>(count + 1)
}
```

`<-` is exclusively a state update. It may target an ordinary register, an
`out reg`, or a supported indexed vector-register element. An ordinary `out`
wire is never stored and therefore cannot be updated with `<-`. A transient
rule/FSM output uses `drive` instead:

```zlang
out fired : bit

rule publish when accept {
    drive fired = 1
}
```

The driven wire is zero when no owning action fires. It participates in the
same atomic scheduling, conflicts and explicit priority as the rule's other
effects. `drive` cannot target a register or `out reg`, and a registered output
cannot also have a combinational assignment. Because changing a wire update to
stored state is a semantic choice, the compiler reports both alternatives but
does not apply an automatic editor fix.

`drive` is a logical transient output assignment. It does not model a
tri-state driver, high impedance (`Z`), electrical or pad drive strength,
analog behavior, or Verilog multiple-driver resolution. Its zero default is
therefore pulse/event semantics, not retained state.

| Form | Cycle semantics | What it is not |
| --- | --- | --- |
| `out value : T` with `value = expression` | Continuously defined combinational value | Stored state |
| `out reg value : T [@clock] [= reset]` with `value <- expression` | Clocked architectural state that holds when not updated | Verilog's historical output declaration category |
| `drive value = expression` in an action | Scheduled transient wire effect; zero when no owning action fires | Tri-state/electrical drive or implicit storage |

Named compatibility spelling remains supported:

```zlang
rule increment_count when increment {
    count <- truncate<8>(count + 1)
}
```

Action blocks may select effects recursively with runtime `when`, `else when`,
and `else`:

```zlang
fault_update: when fault {
    when valid_state {
        overflow_state <- 1
    } else {
        valid_state <- 1
        overflow_state <- 0
        sticky_state <- fault_code
    }
} else when clear_valid {
    valid_state <- 0
    overflow_state <- 0
} else when clear_overflow {
    overflow_state <- 0
}
```

An `else` binds to the nearest unmatched `when`, and an `else when` chain
selects its first true predicate. A false `when` without an `else` contributes
no effects; later statements in the enclosing action block are still
considered, so multiple sibling conditionals can contribute to the same
transaction.

Read this as two operations that never feed back into each other: predicates
first choose the effects, then the scheduler either commits the complete
chosen transaction or commits nothing. Storage readiness and rule conflicts
belong only to the second operation; they can suppress a transaction but can
never make an `else` branch run.

Every guard and operand reads the same pre-edge snapshot. The complete tree
retains one outer `Rule`, one rule-fire identity, and one `ActionGroup`; selected
effects and unconditional surrounding effects commit together or not at all.
If no effect is active, the rule does not fire. Source order creates neither
state visibility nor implicit priority, and reset suppresses the complete
group.

Branch choice depends only on the predicates. If the selected branch contains
an illegal FIFO or memory action, the whole action group is suppressed; it does
not fall back to an `else` branch because another branch's storage action would
be ready. The scheduler checks conflicts and storage legality using only active
effects. Explicit `drive` output writes are scheduling resources too: a rule that loses
a priority conflict on an output cannot commit its otherwise unrelated state
effects.

Each runtime action condition must have exact type `bit`. Opposite structured
branches may write the same resource, but writes on potentially overlapping
paths are rejected; the compiler does not attempt arbitrary Boolean theorem
proving. An explicitly empty branch is a valid no-op, while an entire action
tree with no possible effect is rejected.

Runtime action selection is distinct from both other selection forms:

- compile-time `if` chooses declarations or values during elaboration and is
  not an action statement;
- `condition ? a : b`, `mux`, and `switch` select pure combinational values;
- runtime `when` selects effects that participate in one cycle-level atomic
  transaction.

Rules read beginning-of-cycle state. If the guard is true, all resource actions
are legal, and scheduling permits the rule, its actions commit atomically. Reset
suppresses all rules. Conflicting writers require explicit priority:

```zlang
priority {
    clear: when clear_request { count <- 0 }
    increment: when increment_request {
        count <- truncate<8>(count + 1)
    }
}
```

Priority suppresses a lower enabled rule only when it conflicts with a fired
higher rule. It is not procedural `if`/`else`: nonconflicting enabled rules may
fire together.

A simple ordered chain may instead be written as:

```zlang
priority first > second > third
```

This syntax expands to the adjacent edges `first > second` and
`second > third`. Only declared edges participate; source order adds no
priority. Use the block/pair form for any other priority graph.

<a id="reference-sequential-state-storage-fifos"></a>
## FIFOs

```zlang
fifo queue : fifo<u8,4>
queue.data = rx.payload
queue.push = rx.transfer
queue.pop = tx.transfer
```

Read-only observations include `.front`, `.count`, `.full`, `.empty`, `.ready`,
`.valid`, `.overflow`, and `.underflow`. A legal simultaneous pop/push while
full preserves occupancy and ordering. An empty pop cannot consume a same-cycle
push.

FIFO depth may be a positive compile-time value expression. Backends receive the
resolved integer, not a hardware parameter.

Rules can instead own FIFO operations atomically:

```zlang
rule replace when rotate {
    queue.pop()
    queue.push(queue.front)
    accepted <- 1
}
```

Rule-owned FIFO actions and globally driven `.push`/`.pop` controls are mutually
exclusive ownership forms. An illegal resource action suppresses the whole
rule.

Rule scheduling selects the lexicographically highest-priority **legal atomic
set**, not each rule independently. Thus a push on a full FIFO may fire with a
compatible lower-priority pop on the same edge; a push alone remains blocked.
The compiler constructs exact selection regions for empty, interior, and full
occupancy without enumerating every count or rule subset. If exact scheduling
exceeds its bounded work budget, compilation reports
`ZL-STATE-SCHEDULER-LIMIT` rather than emitting an approximate circuit.

<a id="reference-sequential-state-storage-writable-memories"></a>
<a id="reference-storage-memories"></a>
## Writable memories

One-read/one-write memories may optionally expose a byte write mask. The
existing one-cycle, reset-cleared form remains the default:

```zlang
in write_mask : bits<4>
memory table : mem<u32,256> {
    read_latency 1
    collision write_first
}
table.read_address = read_address
table.write_enable = write_enable
table.write_address = write_address
table.write_data = write_data
table.write_mask = write_mask
```

The mask type is exactly `bits<ceil(element_width / 8)>`.
`write_mask[0]` controls the least-significant byte. For a width that is not a
multiple of eight, the final mask bit controls only the remaining live
most-significant bits; padding bits are never stored. Disabled lanes retain
their old bits. For a same-address `write_first` access, the registered read
result is the post-mask merged word, not the unmasked input. Omitting
`write_mask` preserves the existing full-word write behavior. Consequently,
unmasked and masked memory elements may have any positive recursively
bit-packable, non-enum width.

Rule-owned memories accept the same operation as an optional third operand:

```zlang
rule store when enable { table.write(address, data, byte_mask) }
```

Within a masked rule-owned memory, the compatible two-operand form
`table.write(address, data)` means an all-lanes write. The mask does not add a
read enable, another port, a new clock domain, or target-specific BRAM mapping.

`read_latency` counts active edges in the memory's read domain between a
captured address and the visible result. These are compiler/simulator/Direct-SV
contracts, not promises that a particular FPGA RAM primitive will be inferred:

| Storage form | Accepted `read_latency` | Tested behavior and physical boundary |
| --- | --- | --- |
| Globally controlled `mem`, including named same-clock ports | `0..16` | `0` is combinational; `1` is registered; `2..16` add exact read-domain register stages. Cycle tests cover `0`, `1`, `2`, and the upper bound `16`; Direct-SV is linted. |
| Rule-owned legacy `mem` | Exactly `1` | A selected read action captures at an edge and holds its result otherwise; rule/storage simulations and Direct-SV tests cover it. |
| `async_mem` 1W1R | `1..16` | Reader-domain registered output; cycle tests cover `1`, `2`, `3`, and `16`, including unrelated clocks. Generic Direct-SV supports `old`; stronger cross-clock collision claims need exact target evidence. |
| Immutable `rom` | Exactly `1` | Synchronous table read; compile, simulation, and Direct-SV tests cover it. |
| Internal memory of `async_fifo` | Fixed `1` | The FIFO prefetch controller accounts for the registered read; this is not a user-settable FIFO latency. |

The current explicit Xilinx 7-Series synchronous-memory physical route accepts
only `read_latency 1` and matching reset, mask, port, and collision capability.
Other legal latencies can use generic RTL under a preferred policy; a required
native route rejects them. `read_latency 0` never claims synchronous BRAM.

<a id="reference-sequential-state-storage-named-same-clock-ports"></a>
### Named same-clock ports

Leaving the body without port declarations preserves the legacy controls and
their generated behavior. A memory may instead declare up to eight named
logical ports:

```zlang
memory table : mem<u32,1024> @clk {
    read_write_port a
    read_write_port b
    init RESET_WORD
    read_latency 1
    collision old
    write_priority a > b
}

table.a.read_enable = a_read
table.a.write_enable = a_write
table.a.address = a_address
table.a.write_data = a_data
a_data_out = table.a.read_data
```

`read_port`, `write_port`, and `read_write_port` state the exact access shape.
All named ports of an ordinary `mem` have one resolved clock domain. When more
than one port can write, `write_priority` must list every writer exactly once.
Priority affects only writes to the same address; simultaneous writes to
different addresses both commit. `collision old`, `new`, and `no_change`
select the same-clock registered-read result. The older spellings
`read_first` and `write_first` remain accepted aliases for `old` and `new`.

Named-port masks retain the arbitrary-bitwidth lane rule above. Mixed port
widths and asymmetric depths are rejected. Rule-local memory actions remain a
property of the legacy implicit 1R1W form and cannot be mixed with named ports.

An optional `init VALUE` supplies one exact compile-time element value for
every cell. It may be a typed module value parameter. With `contents clear`,
generic/register-array hardware writes that value to every cell on reset. With
`contents preserve`, it is only the deterministic power-up value; FPGA block
RAM maps it to bitstream initialization and does not pretend that a runtime
reset rewrites the array. Omitting `init` retains the exact zero value used by
the existing reset/initialization policies. Full per-address images remain the
role of immutable `rom` in the current language contract.

When a memory exactly matches a target resource, that resource is used. A
`1W+nR` shape otherwise becomes coherent replicated 1R1W storage; other
same-clock multiwrite shapes may use a deterministic register array, read
muxes, and priority gates when the logical storage is at most 4096 bits.
Larger unsupported shapes fail with a diagnostic recommending explicit
banking, arbitration, or standard-library composition. No automatic banking is
performed because arbitrary same-bank collisions need a protocol contract.

<a id="reference-sequential-state-storage-explicit-dual-clock-memory"></a>
### Explicit dual-clock memory

`async_mem<T,N>` is a separate, deliberately narrow semantic resource:

```zlang
memory table : async_mem<u32,1024> {
    write_port wr @write_clk
    read_port rd @read_clk
    init INITIAL_WORD
    read_latency 1
    collision old
    reset { contents preserve read_data clear }
}

table.wr.enable = write_enable
table.wr.address = write_address
table.wr.data = write_data
table.wr.mask = write_mask
table.rd.address = read_address
read_data = table.rd.data
```

It has exactly one write-only port and one read-only port in different explicit
domains. Cells change only on the writer edge; `rd.data` changes only on the
reader edge and has destination-domain provenance. The contents reset policy
belongs to the write domain and the read-result policy to the read domain.
Direct use of foreign-domain controls is rejected; a pipeline register is not a
CDC primitive.

`async_mem` requires `read_latency` in `1..16`. The generic implementation
captures the cell on the reader edge and, for latency greater than one, shifts
the captured value through reader-domain output registers. This adds visible
latency without changing the contents or crossing domains. `read_latency 0`
is invalid for `async_mem`; it would be an unregistered cross-clock read, not a
block-RAM read port.

For coincident logical edges the simulator uses a pre-edge snapshot: `old`
returns the prior cell, while `new` forwards the coincident write. This is a
precise digital model, not an analog metastability or silicon timing guarantee.
Portable generic Direct-SV publishes `old`; stricter or other
collision claims require an exact target capability. Asynchronous 2RW,
mixed-width ports, ECC, and dual-clock ROM remain deferred.

```zlang
memory table : mem<u8,16> {
    read_latency 1
    collision read_first
}

table.read_address = read_address
table.write_enable = write_enable
table.write_address = write_address
table.write_data = write_data
read_data = table.read_data
```

Alternatively, one memory can be owned by atomic rules:

```zlang
rule fetch when read_enable {
    table.read(address)
    accepted <- 1
}

rule store when write_enable {
    table.write(address, write_data)
}
```

`read` registers `table.read_data` one cycle later and it holds when no read
fires. `write` commits at the selected edge. One selected read and one selected
write may fire together; same-address behavior follows the declared collision
mode. Their operands and all other rule effects observe the same pre-edge
snapshot. Reset suppresses actions. With the default profile it clears both
cells and `read_data`.

Global controls and rule actions cannot be mixed. The bounded scheduled form
supports one memory per module, alongside registers and scheduled FIFOs; two
reads or two writes conflict and require existing explicit rule priority.

Executable memory elements may be recursively bit-packable non-enum aggregates,
as well as scalars; depth is a power of two and at least two. A globally
controlled ordinary `mem` accepts `read_latency` in
`0..16`: zero is a combinational read; each positive value is an exact number
of domain-local read edges from address capture to the visible result.
Rule-owned memories remain exactly one-cycle because their read action is
selected at an edge. Zero-cycle ordinary `mem` is a generic combinational
storage behavior, not a native synchronous BRAM claim. Native target binding
requires an explicitly matching clocked-read capability. Until a target
mapping proves its internal output-register configuration and any additional
fabric stages, a requested latency greater than the advertised native latency
uses generic storage under a preferred policy or fails under a required policy.

Cell and visible read-result reset behavior may be selected independently:

```zlang
memory table : mem<u32,256> {
    read_latency 0
    collision read_first
    reset {
        contents preserve
        read_data preserve
    }
}
```

If the `reset` block is present, both directives are required. If it is absent,
the exact default is `contents clear` and `read_data clear`. Reset always
suppresses writes and scheduled actions. At latency zero, `read_data clear`
masks the combinational result while reset is asserted; `read_data preserve`
leaves the addressed preserved cell visible. At latency one, the corresponding
policy clears or holds the result register. `read_first` observes the old cell
before a coincident write edge; `write_first` observes the fully
byte-mask-merged write word.

The simulator and generic Direct-SV initialize executable preserved
memory state to its declared uniform `init` word, or zero when omitted, but
runtime reset does not recreate that initialization under `contents preserve`.
The Xilinx same-clock true-dual mapping therefore requires preserved contents;
Vivado maps its source initializer to BRAM initialization. Target selection
stays fail-closed when a resource does not advertise initialization support.
Full/partial writable-memory images, automatic banking, unbounded
multiport memory, and asynchronous 2RW are not part of this hardware surface.
Bounded named-port selection is described above. The
[writable-memory contract](sequential-state-storage.md#reference-sequential-state-storage-writable-memories)
defines the hardware boundary.

<a id="reference-sequential-state-storage-replicated-read-ports-wrappers-and-banking"></a>
### Replicated read ports, wrappers, and banking

Named ports now provide a bounded native logical model; source composition is
still preferable when banking or arbitration policy is architecturally
significant. The validated
[`ZtpuBankedMemory`](../examples/ztpu_banked_memory.zhl) uses four
banks and two read replicas per bank: both replicas receive the same decoded,
byte-masked synchronous write, while each read address selects its own replica
and bank. This produces eight distinct physical 1R1W memories, one shared leaf
specialization, two combinational read results, and one logical write port.

The concrete witness contains 256 32-bit words, split into four banks of 64
words. Its runtime reset suppresses writes and preserves both cells and
combinational read data; executable simulation begins from deterministic zero
contents. The semantic simulator and Direct-SV agree on full and
per-byte writes, independent reads, same-address `read_first` behavior, reset,
and replica coherence. This is source composition, not a new memory semantic or
backend name-based rewrite. The ordinary stdlib modules
`StorageDualPortMemory`, `Storage2R1WMemory`, and `StorageAsyncMemory1W1R`
expose common shapes while lowering through the same memory IR.
`StorageAsyncFifo` similarly wraps the existing ready/valid `async_fifo(D)`
crossing; a normal `fifo<T,N>` never becomes asynchronous.

The FIFO crossing now decomposes into an internal typed `async_mem` with
independent write/read clocks, `read_latency 1`, and a one-slot prefetch
controller. It is not a second public memory declaration in the module.
The selected AMD 7-Series `Xilinx7AsyncFifoRAMB18` or
`Xilinx7AsyncFifoRAMB36` architecture binds only this internal FIFO memory
to a matching independent-clock 1W1R RAM with `DO_REG=0`; unsupported
width/depth/configuration fails under `required` policy and remains generic
RTL under `preferred`. This route does not imply that arbitrary public
`async_mem` has a native cross-clock collision guarantee.

Target-memory closure remains separate. In particular, the existing promoted
OpenRAM/target macro contract does not yet match this zero-latency,
preserve-on-reset profile, so the witness makes no BRAM/SRAM inference, QoR, or
physical-macro claim. A byte-addressed external wrapper and target-specific
latency/collision adaptation are not currently supported by this mapping.

<a id="reference-sequential-state-storage-initialized-synchronous-roms"></a>
## Initialized synchronous ROMs

An immutable lookup table is declared separately from writable memory:

```zlang
rom twiddles : rom<Complex<Twiddle>, N / 2> {
    read_latency 1
    init fft_twiddles<T=Twiddle,N=N>()
}

twiddles.read_address = phase
selected_twiddle = twiddles.read_data
```

The initializer must specialize at compile time to exactly
`vec<depth,element_type>`. Elements may be bit-packable scalar/fixed values,
vectors, or concrete structs; protocol/state values and nominal enums are
rejected. Depth is positive and may be one or non-power-of-two. The address is
an unsigned `uint<max(1,ceil_log2(depth))>` value whose proven range remains
inside the declared depth.

The read is a **one-cycle synchronous read**. During reset the registered read
result is zero. Reset never changes the immutable contents; after reset, the
first non-reset result corresponds to the preceding accepted non-reset address.
There is no hidden second output register.

Generated SystemVerilog uses one companion image. It contains
one exact-width binary word per line, address zero first. Struct declaration field zero
occupies the most-significant bits; vector element zero and tuple item zero
occupy the least-significant component bits recursively. Fixed-point values retain
their raw signed or unsigned bit pattern. Direct-SV `$readmemb` consumes that
image. The CLI publishes companions
beside the selected output, and artifact metadata
retains the initialization dependency, evaluator, content, and file hashes.
Missing or colliding companion files fail closed.

Writable or partial initialization, reloadable/asynchronous/multiport ROM,
enum elements, and automatic BRAM/storage exploration remain deferred.

<a id="reference-sequential-state-storage-stateful-hierarchy"></a>
## Stateful hierarchy

Single-domain scalar child modules may contain registers and rules. A
compile-time indexed instance array may contain bounded scalar sequential or
primitive ready/valid children with one matching clock/reset domain; each
physical instance has independent state and reset. Aggregate scalar outputs and
mixed scalar/ready-valid ports retain their exact typed leaves. Storage-only
child arrays may own one FIFO, synchronous memory, or initialized ROM. The
scheduled-FIFO profile may also combine FIFO actions with ordinary register
writes/rules because both use the same `ResolvedTransition`; legacy globally
controlled storage plus user state remains rejected.

One outer array may contain a bounded same-domain scalar or direct-ready-valid
hierarchy, including an inner scalar compile-time array. First-level in-order
request/response requester/responder arrays are supported with scalar wire peers
and explicit indexed connections. Nested storage/CSR/aggregate protocols,
nested or out-of-order request/response, other non-RV protocols, CDC,
runtime-selected instances, and cross-module atomic scheduling remain
fail-closed. See
[Hierarchy and protocols](hierarchy-protocols.md#reference-hierarchy-protocols-modules-and-instances).
