# Sequential logic, rules, and storage

ZLang keeps combinational values, next-state actions, timing, and storage
explicit in typed IR. Synchronous active-high reset is the default; an explicit
single-domain asynchronous contract is available when the physical interface
requires it.

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
port, state, and connection associations:

```zlang
clock source_clock
reset source_reset @source_clock
```

Implicit clock-domain crossing is rejected. See
[Hierarchy and protocols](hierarchy-protocols.md#clock-domain-crossings).

The recommended asynchronous spelling asserts immediately and releases only
after two active clock edges:

```zlang
clock clk
async reset arst_n @clk { polarity active_low }
```

Normal state transition resumes on the third edge after external deassertion.
The low-level `reset ... { mode asynchronous ... }` spelling retains raw native
deassertion for compatibility. Exact polarity, falling-edge behavior,
hierarchical propagation, and unsupported formal/target routes are documented
in the [physical clock/reset contract](physical-clock-reset-contract.md).

## Registers and next state

```zlang
reg count : u8 = 0
count <- truncate<8>(count + 1)
```

Expressions read committed beginning-of-cycle state. All accepted `<-` updates
become visible together at the active edge. A register without an accepted
update holds. Reset restores the declared constant. Multiple unordered writers
are rejected.

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

The simulator, Clash, and direct SystemVerilog share the declaration-order
ordinal encoding. The source-authored APB bridge in
[`stdlib/bus/apb.zhl`](../stdlib/bus/apb.zhl) uses an enum for its internal phase
without exposing it through the external bus ABI.

For control-oriented state, `fsm` is concise syntax for the same enum register
and guarded atomic rules:

```zlang
enum TxPhase { Idle Header Payload Done }

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

## Delays and fixed pipelines

```zlang
delayed = delay<2>(value)
piped = pipeline(3) { expression }
```

`delay<N>` adds exactly `N` zero-reset scalar stages. `pipeline(N)` evaluates the
body and adds exactly `N` zero-reset output stages; it does not imply a particular
internal partition. Both accept one value per cycle after fill. Operands at an
operator must be latency-aligned; the compiler does not silently add balancing
registers.

`pipeline(auto)` is a separate, bounded implementation-planning form described
in [Optimization and formal verification](optimization-formal.md).

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

The contract describes observable behavior; it does not insert registers or
authorize retiming. Constants are timeless, ordinary input-dependent logic is
known at latency zero, and explicit `delay<N>`/`pipeline(N)` adds `N`. Timeless
values may join a known path. All other non-timeless operands at a join must
have the same known latency.

Registers, rules, FIFO/memory/ROM observations, protocols, and uncontracted
child outputs are `unknown` for this first public contract. A positive latency
requires exactly one clock/reset domain. Only `ii 1` is accepted. A contracted
child adds its declared latency to the common known latency of its bound scalar
inputs; an unaligned parent join is rejected instead of silently balanced.

This is distinct from `pipeline(auto)` constraints and target evidence. A
planner or profile must preserve an exact module contract as immutable public
behavior.

## Guarded atomic actions

The concise action form is:

```zlang
when increment {
    count <- truncate<8>(count + 1)
}
```

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
effects. Scalar output writes are scheduling resources too: a rule that loses
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

This syntax expands to the existing adjacent edges `first > second` and
`second > third`. It does not invent extra edges or infer priority from source
order; retain the block/pair form for any other priority graph.

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

Do not mix rule-owned actions with globally driven `.push`/`.pop` controls. An
illegal resource action suppresses the whole rule.

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

The executable memory forms have scalar elements and power-of-two depth of at
least two. A globally controlled memory accepts `read_latency 0` for a
combinational read or `read_latency 1` for a registered result. Rule-owned
memories remain exactly one-cycle because their read action is selected at an
edge.

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
before a coincident write edge; `write_first` observes the fully byte-mask-
merged write word.

The simulator and both generic RTL backends initialize executable preserved
memory state deterministically to zero, but runtime reset does not recreate
that initialization. Target-specific selection stays fail-closed: the current
BRAM mapping advertises only the default one-cycle clear/clear profile.
Synthesizable writable-memory initialization, multiport memory, and automatic
target-memory selection are not part of this hardware surface. Simulation-only
tests may instead publish a selected-IR-bound state catalog and Verilator VPI
companion with `--simulation-state-bundle`; that tooling neither adds hardware
ports nor changes memory reset semantics. The
[writable-memory contract](#writable-memories) and public
[simulation-state access](direct-systemverilog.md#simulation-only-architectural-state-access)
sections define the two distinct boundaries.

### Replicated read ports and banking

The language does not invent a native multiport memory primitive. A logical
two-read/one-write store can instead be expressed compositionally from the
existing one-read/one-write memory and compile-time instance arrays. The
validated [`ZtpuBankedMemory`](../examples/ztpu_banked_memory.zhl) uses four
banks and two read replicas per bank: both replicas receive the same decoded,
byte-masked synchronous write, while each read address selects its own replica
and bank. This produces eight distinct physical 1R1W memories, one shared leaf
specialization, two combinational read results, and one logical write port.

The concrete witness contains 256 32-bit words, split into four banks of 64
words. Its runtime reset suppresses writes and preserves both cells and
combinational read data; executable simulation begins from deterministic zero
contents. Simulator, direct SystemVerilog, and real Clash 1.11 agree on full
and per-byte writes, independent reads, same-address `read_first` behavior,
reset, and replica coherence. This is source composition, not a new memory
semantic or backend name-based rewrite.

Target-memory closure remains separate. In particular, the existing promoted
OpenRAM/target macro contract does not yet match this zero-latency,
preserve-on-reset profile, so the witness makes no BRAM/SRAM inference, QoR, or
physical-macro claim. A byte-addressed external wrapper and target-specific
latency/collision adaptation belong to later ZTPU integration work.

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

Backends consume one compiler-owned companion image. It contains one exact-
width binary word per line, address zero first. Struct declaration field zero
and vector element zero occupy the most-significant bits, recursively, while
fixed-point values retain their raw signed or unsigned bit pattern. Direct
SystemVerilog `$readmemb` and Clash `romFile` consume byte-identical images.
The CLI publishes companions beside the selected output, and artifact metadata
retains the initialization dependency, evaluator, content, and file hashes.
Missing or colliding companion files fail closed.

Writable or partial initialization, reloadable/asynchronous/multiport ROM,
enum elements, and automatic BRAM/storage exploration remain deferred.

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
[Hierarchy and protocols](hierarchy-protocols.md#modules-and-instances) and
[storage-owning instance arrays](storage-instance-arrays.md).
