<a id="reference-sequential-state-storage"></a>
## State, clocks, rules, FSMs, and pipelines

ZLang keeps combinational values, next-state actions, timing, storage, and
physical clock ownership explicit in source. Synchronous active-high reset is
the default. Each independently declared clock has one reset contract and may
own ordinary state in the same module.

<a id="reference-sequential-state-storage-clock-and-reset"></a>

### Clock and reset

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
[Hierarchy and protocols](#reference-hierarchy-protocols-clock-domain-crossings).

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
in the [physical clock/reset contract](#reference-sequential-state-storage).

<a id="reference-sequential-state-storage-registers-and-next-state"></a>

### Registers and next state

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
visualization: each JSONL event occupies two 1 ns trace ticks, reset changes
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
[`stdlib/bus/apb.zhl`](../../stdlib/bus/apb.zhl) uses an enum for its internal phase
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
#### Runtime-indexed vector register updates

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

### Delays and fixed pipelines

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

### Exact module timing contracts

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

### Guarded atomic actions

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

### Recommended asynchronous reset

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
[Registers and next state](#reference-sequential-state-storage-registers-and-next-state).

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
