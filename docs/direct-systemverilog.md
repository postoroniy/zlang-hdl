# Direct SystemVerilog supported-secondary backend

Clash remains ZLang's primary backend.  Direct SystemVerilog is a supported
secondary backend for the validated subset below; acceptance is fail-closed.
Every source accepted by the direct emitter in the example regression is
required to pass real Verilator lint.

The backend consumes typed semantic IR.  It never dispatches on AXI, APB, or
RegBus module names: source-authored standard-bus components use the same
aggregate endpoint, hierarchy, register, rule, and connection lowering as user
modules.

Ordinary scalar register/rule components share the standalone/composed dispatch
and register contributor. Independent rules do not require global scheduler
enumeration merely because a module is instantiated as a child. Hierarchy
validation is cached only within one emission; protocol/storage effects and
conflicting multi-effect rules keep their existing exact paths. ZL-017's composed
AHB/queued Q16 witness now emits in about 16.4 seconds on the recorded host.
Generated private names now use a shared hierarchy-local allocation plan:
`cfg_ready`, `lane_0`, `lane_0_value`, `result_pipe_s1`, and short specialized
definitions such as `Counter_s1a2b3c4d`. Source names win over generated helpers;
actual collisions receive a deterministic suffix. Public top/port ABI and
source register names are unchanged, including existing public aggregate-leaf
separators. BackendArtifact records the naming schema and complete semantic
identities separately from regenerated RTL/VPI locators. Clash consumes the
same policy for its source helpers; final Clash-generated HDL internals remain
controlled by Clash. Naming does not change circuit semantics or imply QoR gains.

Formal-only `rule.fire` now uses the same effective reset and polarity as
production state, including the two-edge synchronized release and conditioned
child reset. Real RTL covers both polarities, both active edges and nested
hierarchy. Its separate global-guard enumeration scalability limitation remains:
large independent-rule formal projections still require a future bounded fix.

Fixed-point ports and expressions use the canonical scaled-integer types behind
`fixed`/`ufixed`, concise `SF`/`UF`, and `_Sat` formats. Same-scale target
narrowing and explicit rescaling are both emitted from `FixedConvert`; the
backend never infers wrap, saturation, or rounding from source spelling.

## Supported composition subset

- scalar and packed struct/vector/tuple datapaths, `char`/`string<N>` aliases,
  immutable locals, muxes, field/projection access, compile-time and proven
  runtime packed-bit indexing with LSB-zero semantics, exact-width `bitcast`,
  homogeneous vector `concat`, compile-time `reshape`, reductions, fixed-point
  arithmetic/conversion, and selected or nested fixed-latency scalar pipelines
  with II=1;
- registers, next-state assignments, atomic rules, reset, and priority,
  including one-domain `async reset` with one root-owned two-register
  asynchronous-assert/synchronous-release conditioner;
- reusable parameter specializations, distinct scalar physical child
  instances, and compile-time indexed combinational or single-domain
  sequential instance arrays, including aggregate scalar outputs and scheduled
  FIFO state combined with ordinary child registers/rules;
- mixed scalar/state/ready-valid/request-response children;
- mixed scalar plus ready/valid instance-array children, bounded nested
  same-domain scalar/direct-ready-valid array hierarchy, and first-level
  indexed in-order request/response requester/responder arrays;
- read-only runtime projection of one exact bit-packable scalar/aggregate wire
  output from an instance array; every physical child remains independently
  instantiated and active;
- the bounded single-input/single-output ready/valid `transform pipeline(auto)`
  with one pure M31 product-reduction kernel and a compiler-owned global stall;
- ready/valid connections and finite connection FIFOs, including simultaneous
  push/pop and backpressure;
- scalar ready/valid `async_fifo(N)` clock crossings and aggregate crossings
  whose schema has exactly one forward ready/valid member; aggregate payloads
  cross as one packed atomic value;
- vendor-neutral scalar `sync_level`, `pulse_toggle`, and one-entry
  ready/valid `handshake` crossings, using the same coordinated-reset and
  latency semantics as the simulator and Clash;
- synchronous memories with the currently typed collision/read-latency model;
- fixed-priority and round-robin packet arbiters with beat- or packet-scoped
  grants;
- in-order hierarchical request/response channels with independent directional
  buffers as represented in typed IR;
- one mandatory public top ABI derived from `TopPhysicalABI`: structs are
  recursively exposed as named field leaves, tuples as deterministic `itemN`
  leaves, while every `vec<N,T>` leaf is a native unpacked SystemVerilog array
  (`T name [0:N-1]`); packed aggregate and vector representations exist only
  behind the private generated core;
- source-authored AXI4-Lite, APB, AXI-Stream, Wishbone, RegBus, and CSR hierarchy
  used by the current standard-library and real-design examples;
- hardware-connected CSR `ro`, `pulse`, and sticky-W1C behavior with the frozen
  software/hardware priority policy;
- ready/valid-to-credit, credit-to-ready/valid, and per-VC credit source state
  machines from typed protocol/capacity metadata;
- BackendArtifact v4 recursive instance/state locators and version-10 physical
  domain contracts for non-default reset modes. Specialization
  identity is not used as physical instance identity.

SystemVerilog reserved words are deterministically prefixed with `zlang_`.
The collision check runs after that physical-name mapping, so two distinct
semantic leaves can never silently become one RTL port. Public-wrapper helper
signals are allocated outside the public namespace and remain deterministic.
Unresolved locals are eliminated before backend lowering.  Unsupported IR
shapes raise `SystemVerilogEmissionError`; no artifact is published after a
failed emission.

Ordered comparisons preserve typed signedness explicitly: both operands of
`<`, `<=`, `>`, and `>=` are rendered through `$signed` or `$unsigned` according
to typed IR, independent of whether an operand is a port, local, projection, or
register. Equality and inequality remain raw bit comparisons.

Use the stable CLI option:

```sh
.venv/bin/zlang examples/simple_dma_m40.zhl --top SimpleDMA \
  --systemverilog build/SimpleDMA.sv
verilator --lint-only --top-module SimpleDMA build/SimpleDMA.sv
```

`--experimental-systemverilog` remains an exact compatibility alias. Both
options are explicit artifact sinks: they write only the requested SV file and
leave stdout empty unless `-o` is also supplied. With both `-o` and a
SystemVerilog path, both requested artifacts are written and stdout remains
empty. A bare `zlang SOURCE` invocation retains the legacy behavior of
printing Clash source to stdout; `--verilog-dir` and all report, IR, contract,
formal, CSR, synthesis, and manifest paths suppress that implicit Clash stream.
Direct-only and `--check` invocations stop before Clash emission, so a feature
supported by direct SV is not rejected by an unrelated Clash limitation.
Selection/configuration flags such as `--top`, `--target`, and formal policy
alone do not suppress the legacy default. Diagnostics remain on stderr.

Use `--verbose` when an explicit success confirmation is useful:

```sh
.venv/bin/zlang design.zhl --systemverilog design.sv --verbose
```

This keeps stdout artifact-safe and reports the selected top and written paths
on stderr. To validate parsing, top selection, and semantic analysis without
publishing any backend artifact, use:

```sh
.venv/bin/zlang design.zhl --check
```

`--check` prints a one-line success result and returns zero. Without `--top` it
checks every module declared in the source file, preventing an invalid dormant
module from being hidden by default-top selection. With `--top`, it checks only
that selected elaboration root. Syntax, semantic, and top-selection failures
retain their normal nonzero status and diagnostic. As a check-only mode, it
cannot be combined with artifact output options.

## Simulation-only architectural state access

A direct-SV build may publish a separate Verilator VPI companion without
adding ports or changing the production RTL:

```sh
.venv/bin/zlang design.zhl \
  --systemverilog build/design.sv \
  --simulation-state-bundle build/state-access
```

The companion manifest covers the selected typed hierarchy and is bound to the
exact emitted artifact hash, build identity, state shapes, and emitter-owned
physical locators. Publication re-hashes the `.sv` file, so a stale or modified
RTL file cannot receive an access manifest. The generated C++ header requires
Verilator `--vpi --public-flat-rw` and validates its complete state allow-list
before access.

The bounded surface includes bit-packable user registers (including vector
registers), writable-memory cells, and the persistent read-result latch of a
one-cycle memory. Values up to 64 bits have convenience methods; arbitrary-
width scalars and elements use exact 32-bit least-significant-word-first arrays.
Vector element zero retains the canonical most-significant packed position.
The persistent semantic simulator consumes the same catalog and keeps one state
object per physical child instance.

This first slice requires one exact clock/reset domain and generic direct-SV.
Target-mapped state, FIFO/CSR/protocol internals, CDC, Clash state mutation,
arbitrary force/release, and synthesizable preload logic remain unavailable.
See the matching [backend-tooling CLI description](backends-tooling.md#core-cli).

## Formal boundary

Real Z3/SymbiYosys regression targets exercise emitted direct RTL for register
transitions and rule priority, ready/valid stability and FIFO accounting, CSR
W1C behavior, and request/response acceptance.  Arithmetic, state-transition,
FIFO-accounting, response-accounting, W1C, and priority mutations all fail with
counterexample metadata.

The first-class verification-bundle path also accepts direct-SV designs with
initialized ROM. Exact memory images are published below
`implementation/companions/` and listed as hash-validated inputs for every SBY
job that consumes them. Generated solver configuration, logs, and VCDs are
retained outside the immutable bundle; run-report v7 maps sampled physical VCD
signals back to semantic binding IDs for witnesses and counterexamples.

Direct SV remains the first executable bundle route. If its formal emission
fails, the bounded structured Clash route finalizes real generated Verilog,
validates public and recursive observation ports, and republishes one common
artifact hash for scalar/public and register-observation cases. Unsupported
aggregate/protocol shapes still report an explicit non-executable reason; no
backend-name reconstruction is used.

Transaction-level RTL simulation found and fixed one source-library issue:
`RegBusCSRTarget` previously asserted its response only in the request-transfer
cycle, before the AXI-Lite/APB frontend entered its response phase.  The
ordinary `.zhl` target now stores response data and holds `response.valid` until
`response.transfer`.  Both source-authored AXI-Lite and APB paths complete real
Verilator write transactions through RegBus after this fix.

The v4 manifest publishes recursive physical locators only for signals actually
present in emitted RTL. Current direct-SV formal emission publishes the typed
accepted-request, directional-buffer occupancy, response-consumption, and
parent-owned outstanding-ledger observations used by the supported recursive
request/response properties. Register, FIFO, CSR, and rule-fire observations
are likewise executable where their complete typed binding sets exist. A
`formal_observation_token` remains absent for hidden or unsupported state, and
the affected property reports `skipped` rather than fabricating a proof.

## Explicitly unsupported

Full burst/ID AXI4, backend-specific CDC primitives,
backend-by-region selection, hierarchical M36, and protocol-level M38
remain outside this backend subset. Direct SV is not promoted to the primary
backend. Compile-time indexed instance arrays are structurally unrolled from
typed IR. Supported bounded profiles include scalar combinational/sequential
children, aggregate scalar outputs, primitive ready/valid children with scalar
wire peers, storage-only FIFO/memory/ROM children, primitive ready/valid
children owning one FIFO, scheduled FIFO actions combined with ordinary
register/rule state, and bounded nested same-domain scalar or direct-ready-valid
hierarchy. The bounded scalar subset also includes the source-composed
`ZtpuBankedMemory`: a scalar wrapper around two flat arrays of 1R1W leaves,
with two scalar outputs returned by one coherent reusable child component. A
first-level request/response array may connect compile-time indexed
requester/responder elements when every child has one `ordering in_order`
endpoint and scalar wire ports only.

Legacy globally controlled storage combined with user state, out-of-order or
nested request/response arrays, additional protocols on a request/response array
child, arbitrary nested storage/CSR/aggregate protocol hierarchy, CDC arrays, and
runtime-selected inputs, protocols, or actions remain fail-closed. The backend
never silently emits only one element or reconstructs an indexed connection
from an RTL name.

Sequential ready/valid modules and role-qualified standalone in-order
`request_response` requester/responder modules use the same typed clock/reset,
ownership, and accounting semantics as their hierarchical forms.  A standalone
endpoint owns a local unbuffered ledger: request transfer increments it,
response transfer decrements it, and reset starts a new empty epoch.  Stateful
generic child templates that still require parent specialization remain
validated through their concrete parents.

## Exhaustive example matrix

The direct-SV regression recursively discovers every `.zhl` file and every
declared module root under `examples/`. There are no emission-error skips. Each
root is classified as standalone-supported, child/template-only, or explicitly
unsupported; an unclassified new root is tested as standalone-supported.

The accepted snapshot contains **84 `.zhl` files / 174 module roots / 157
standalone roots / 17 child or template roots / 0 unsupported roots**. The
registry test remains authoritative when examples change; these numbers are an
evidence snapshot rather than a hard-coded allow-list.

The executable registry reports the current source/root totals during test
collection. Every discovered standalone-supported root emits a BackendArtifact
and passes strict Verilator lint; child/template-only roots are exercised
through a concrete parent or specialization, and no current root requires an
explicit unsupported classification. This avoids turning a documentation count
into a second registry whenever project source units are consolidated.

The child/template registry retains only generic stateful/DMA/FFT children that
still require a concrete specialization or parent ABI, plus canonical Wi-Fi
helpers whose standalone top would expose a nominal enum through an external
raw ABI. Each has one named raw/concrete parent witness. The executable registry in
`tests/systemverilog/test_example_coverage.py` is authoritative; adding a new
root without an explicit child reason makes it standalone-supported by default.

Every currently discovered non-child root is standalone-supported; new roots
enter that class automatically and must emit and lint rather than being
silently skipped. This closes the current example corpus, not every possible IR
shape: unsupported future constructs still fail closed at capability checking.
Publication additionally requires the shared exact-once
`ModuleFeatureInventory` check described in
[backend tooling](backends-tooling.md#emitter-architecture-boundary); a renderer
cannot silently omit or double-own a selected IR entity and still return an
artifact.

The two additional ZTPU templates require concrete type/value parameters and
are therefore child/template-only. Their concrete `ZtpuBankedMemory` parent is
standalone-supported, emits one reusable 1R1W leaf module with eight physical
instances, publishes all recursive instance/storage paths, and passes strict
Verilator lint and cycle simulation. Real Clash 1.11 emits the same typed
component graph and behavior through its bundled multi-output scalar child ABI;
this is not a post-normalization RTL-hierarchy claim.

The additional streaming roots include the staged `FFT4SDFReference` through
`FFT512SDFReference` hierarchy. Direct SV emits one distinct specialization per
stage and deterministic ROM companions at the corresponding depths; FFT512
publishes all nine companions and passes complete oracle-backed RTL simulation.
The persistent backend-independent simulator also runs the same 1,033-cycle
replay by default in about 11 seconds and roughly 84 MiB RSS. Its 512 outputs
match both direct-SV and Clash RTL against the frozen oracle. This validates the
functional hierarchy, not automatic DSP mapping or physical QoR.

`examples/projects/80211a_transmitter` is the first complete converted-IP
project in the corpus. Its executable source is consolidated into the canonical
`data_types`, `controller`, `scrambler`, `conv_encoder`, `interleaver`,
`mapper`, `ifft_library`, `cyclic_extender`, `ifft`, and `transmitter` units.
`Ieee80211aTransmitter` is the stable top; temporary compatibility and
implementation-prefixed roots have been retired from the source tree.

The earlier conversion slices exposed reusable direct-SV fixes: typed FIFO
read-side projections, consistent port-name mangling in RTL and manifests,
exact packed slicing, keyword-safe helper names, recursive closed ready/valid
children, and deterministic materialization of shared expressions. Those
findings remain recorded in the Wi-Fi validation report, but the old roots are
not alternate executable profiles.

The canonical hierarchy exercises packet framing, a stateful scrambler, K=7
encoding, corrected IEEE R1/R2/R4 interleaving, BPSK/QPSK/16-QAM mapping,
pilot state, exact widened DIF-SDF arithmetic, natural-order reorder, and an
80-sample cyclic prefix. It uses only generic typed hierarchy, ready/valid,
rules, vectors, ROM companions, fixed-point operations, and source-level
helpers. No Wi-Fi module-name dispatch or backend-only permutation/IFFT logic
exists.

The compact whole-vector IFFT64 reference is deliberately not added as a full
N=64 RTL corpus root. Its N=64 path is semantic/canonical/simulator evidence;
N=8 and N=16 use the same typed `FunctionalRegion` and exact reduction plan as
physical direct-SV/Clash/Verilator witnesses. The backend does not contain an
IFFT, Complex, or Wi-Fi-specific lowering rule.

The canonical hierarchy is a streaming inverse DIF-SDF design rather
than that whole-vector reference. It composes IEEE-authoritative framing,
scrambling, convolutional coding, corrected R1/R2/R4 interleaving, mapping,
D32/D16/D8/D4/D2/D1, natural-order reorder, and an 80-sample cyclic prefix.
`Ieee80211aTransmitter` passes a complete 903-cycle direct-SV/Verilator packet
replay against the independent oracle. The same replay also passes real Clash
1.11 RTL, so this is functional backend evidence rather than direct-SV-only or
Wi-Fi-specific backend behavior.

Strict lint keeps only non-correctness waivers for declaration filenames and
unused/undriven test-fixture signals. Width, latch, driver, and structural
warnings remain fatal. In particular, all fixed-point conversion constants are
emitted at the exact conversion work width. Writable memories lower their typed
zero/one-cycle read, collision, byte-mask, cell-reset and read-result-reset
profile directly; the omitted profile retains the legacy one-cycle clear/clear
text.

## Internal combinational-driver style

Compiler-generated internal combinational values use a declare-first form:

```systemverilog
logic zlang_push, zlang_pop;
assign zlang_push = input_valid && input_ready;
assign zlang_pop = output_valid && output_ready;
```

The production emitter does not combine declaration and drive as
`wire name = expression`. Exact packed widths and signedness remain on the
`logic` declaration. A simple single-driver equation uses continuous `assign`;
`always_comb` is reserved for grouped logic that requires defaults, branching,
or multiple related procedural assignments. ANSI input/output net declarations,
parameters, formal initial-state variables, testbenches, and user-supplied
external SystemVerilog are not rewritten by this backend style rule.

The example registry checks this invariant across every emitted standalone
root. Additional focused witnesses cover optional FIFO observations, masked
memory writes, packet arbitration, hierarchical FIFO helpers, CDC, and the
simulation-only DSP48E1 helper.

## Validation record

The current acceptance tool versions and zero-skip release minimum are recorded
in [`release/status.json`](../release/status.json). The FFT512
backend-independent replay is now a
routine bounded test rather than an opt-in skip.
Historical per-slice counts remain in their validation records; they are not
the current baseline.
The representative `examples/all_syntax.zhl` language-tour backend audit checks
every declared top separately; exhaustive direct-SV root coverage remains the
separate registry described above:

- Clash generates real Verilog and passes Verilator lint for all 27 tops;
- direct SystemVerilog emits and passes strict Verilator lint for all 27 tops;
- `CdcSyntax` and the single-channel aggregate `AXIStream` crossing use the
  same typed Gray-pointer async-FIFO semantics;
- nested rule expressions containing `delay`/`pipeline`, fixed saturation,
  packet arbitration, clocked contracts, and Clash ports colliding with
  Prelude names have dedicated regressions;
- fixed-priority and round-robin packet arbitration, plus nested state staging,
  execute in real Verilator simulations.

The wider streaming, fixed-point, DMA, and Wishbone tops remain lint-clean in
direct SV and compile through real Clash. Python `compileall` and
`git diff --check` also pass.
