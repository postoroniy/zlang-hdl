<a id="reference-generate-systemverilog"></a>
## Generate and integrate RTL

Write the selected top to an explicit file:

```sh
mkdir -p build
zlang design.zhl --top Top --systemverilog build/Top.sv
```

The command writes only the requested artifact and keeps stdout empty.
Diagnostics and `--verbose` progress messages go to stderr. A bare
`zlang design.zhl --top Top` invocation writes SystemVerilog to stdout, which is
convenient for a pipe but less suitable for a reproducible project script.

Use `--check` when no RTL should be created:

```sh
zlang design.zhl --top Top --check
```

Lint the generated module with Verilator:

```sh
verilator --lint-only --top-module Top build/Top.sv
```

Project profiles can select a target and clock constraint:

```sh
zlang top.zhl --profile release \
  --systemverilog build/Top.sv \
  --constraints-xdc build/Top.xdc \
  --constraints-sdc build/Top.sdc
```

Generated RTL preserves declared widths, signedness, reset modes, hierarchy,
and protocol behavior. Unsupported combinations stop with a diagnostic rather
than emitting a partial module. Public port names are stable; private helper
names are not a source-language interface and should not be referenced by user
code.

Use `--systemverilog` for file output or the bare command for stdout.


The backend consumes typed semantic IR.  It never dispatches on AXI, APB, or
RegBus module names: source-authored standard-bus components use the same
aggregate endpoint, hierarchy, register, rule, and connection lowering as user
modules.

Ordinary scalar register/rule components share the standalone/composed dispatch
and register contributor. Independent rules do not require global scheduler
enumeration merely because a module is instantiated as a child. Hierarchy
validation is cached only within one emission; protocol/storage effects and
conflicting multi-effect rules keep their existing exact paths.
Generated private names now use a shared hierarchy-local allocation plan:
`cfg_ready`, `lane_0`, `lane_0_value`, `result_pipe_s1`, and short specialized
definitions such as `Counter_s1a2b3c4d`. Source names win over generated helpers;
actual collisions receive a deterministic suffix. Public top/port ABI and
source register names are unchanged, including existing public aggregate-leaf
separators. BackendArtifact records the naming schema and complete semantic
identities separately from regenerated RTL/VPI locators. Naming does not change
circuit semantics or imply QoR gains.

Formal-only `rule.fire` now uses the same effective reset and polarity as
production state, including the two-edge synchronized release and conditioned
child reset. Real RTL covers both polarities, both active edges and nested
hierarchy. Its separate global-guard enumeration scalability limitation remains:
large independent-rule formal projections remain outside the scalable supported subset.

Fixed-point ports and expressions use the canonical scaled-integer types behind
`fixed`/`ufixed`, concise `SF`/`UF`, and `_Sat` formats. Same-scale target
narrowing and explicit rescaling are both emitted from `FixedConvert`; the
backend never infers wrap, saturation, or rounding from source spelling.

<a id="reference-direct-systemverilog-supported-composition-subset"></a>

### Supported composition subset

- scalar and packed struct/vector/tuple datapaths, `char`/`string<N>` aliases,
  immutable locals, muxes, field/projection access, range-proven runtime packed-bit
  indexing/fixed-width packed slices and runtime vector indexing with LSB-zero semantics, exact-width `bitcast`,
  homogeneous vector `concat`, compile-time `reshape`, reductions, fixed-point
  arithmetic/conversion, and selected or nested fixed-latency scalar pipelines
  with II=1;
- registers, next-state assignments, atomic rules, FSM lowering, reset, and
  priority in one or more explicitly owned domains, including one independent
  two-register asynchronous-assert/synchronous-release conditioner per
  applicable domain;
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
  with one pure pipeline scheduling product-reduction kernel and a compiler-owned global stall;
- ready/valid connections and finite connection FIFOs, including simultaneous
  push/pop and backpressure;
- scalar ready/valid `async_fifo(N)` clock crossings and aggregate crossings
  whose schema has exactly one forward ready/valid member; aggregate payloads
  cross as one packed atomic value;
- vendor-neutral scalar `sync_level`, `pulse_toggle`, and one-entry
  ready/valid `handshake` crossings, using the same coordinated-reset and
  latency semantics as the simulator;
- synchronous memories with the currently typed collision/read-latency model;
- fixed-priority and round-robin packet arbiters with beat- or packet-scoped
  grants;
- in-order hierarchical request/response channels with independent directional
  buffers as represented in the checked design;
- one mandatory public top ABI derived from `TopPhysicalABI`: structs are
  recursively exposed as named field leaves, tuples as deterministic `itemN`
  leaves, while every `vec<N,T>` leaf is a multidimensional packed
  SystemVerilog array (`logic [N-1:0][W-1:0] name` for a `W`-bit element);
  flat packed aggregate representations exist only as deterministic aliases
  inside the selected top or inside compact child components;
- source-authored AXI4-Lite, APB, AXI-Stream, Wishbone, RegBus, and CSR hierarchy
  used by the current standard-library and real-design examples;
- hardware-connected CSR `ro`, `pulse`, and sticky-W1C behavior with the frozen
  software/hardware priority policy;
- ready/valid-to-credit, credit-to-ready/valid, and per-VC credit source state
  machines from typed protocol/capacity metadata;
- recursive instance/state locators and exact physical-domain contracts for
  non-default reset modes. Specialization
  identity is not used as physical instance identity.

SystemVerilog reserved words are deterministically prefixed with `zlang_`.
The collision check runs after that physical-name mapping, so two distinct
semantic leaves can never silently become one RTL port. Inline-boundary helper
signals are allocated outside the public namespace and remain deterministic.
Packed dimensions use conventional descending ranges. ZLang element zero maps
directly to physical packed index zero and the least-significant private slice.
The generated artifact has one unconditional top definition and
is accepted by both Yosys and Verilator without backend preprocessor branches.
Unresolved locals are eliminated before backend lowering.  Unsupported IR
shapes raise `SystemVerilogEmissionError`; no artifact is published after a
failed emission.

Ordered comparisons preserve typed signedness explicitly: both operands of
`<`, `<=`, `>`, and `>=` are rendered through `$signed` or `$unsigned` according
to the checked design, independent of whether an operand is a port, local, projection, or
register. Equality and inequality remain raw bit comparisons.

Use the stable CLI option:

```sh
zlang examples/simple_dma.zhl --top SimpleDMA \
  --systemverilog build/SimpleDMA.sv
verilator --lint-only --top-module SimpleDMA build/SimpleDMA.sv
```

`--systemverilog` is an explicit artifact sink: it writes only the requested
SV file and leaves stdout empty. A bare `zlang SOURCE` invocation emits the production
Direct-SV artifact to stdout. Diagnostics remain on stderr.

Use `--verbose` when an explicit success confirmation is useful:

```sh
zlang design.zhl --systemverilog design.sv --verbose
```

This keeps stdout artifact-safe and reports the selected top and written paths
on stderr. To validate parsing, top selection, and semantic analysis without
publishing any backend artifact, use:

```sh
zlang design.zhl --check
```

`--check` prints a one-line success result and returns zero. Without `--top` it
checks every module declared in the source file, preventing an invalid dormant
module from being hidden by default-top selection. With `--top`, it checks only
that selected elaboration root. Syntax, semantic, and top-selection failures
retain their normal nonzero status and diagnostic. As a check-only mode, it
cannot be combined with artifact output options.

<a id="reference-direct-systemverilog-simulation-only-architectural-state-access"></a>

### Explicitly unsupported

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

<a id="reference-direct-systemverilog-exhaustive-example-matrix"></a>

### Example and structural coverage

The release checks every standalone-supported example root through Direct-SV
emission and strict Verilator lint. Child/template roots require a concrete
specialization or parent. These tests are evidence for the shipped examples, not
a language allow-list; unsupported new combinations fail closed. Current corpus
counts and tool versions are published in
[release status](../../release/status.json).

The streaming FFT and IEEE 802.11a transmitter examples exercise typed
hierarchy, ROMs, fixed-point datapaths, protocols and state. Their RTL is built
through the same generic backend path as other designs, without module-name
dispatch. Selected examples also have independent cycle-level oracle checks.

Generated RTL uses exact packed widths and single-driver combinational
assignments. Procedural combinational blocks are used where defaults or
branching require them; strict lint treats width, latch, driver and structural
warnings as failures. A target binding is claimed only when the selected
physical capability proves its clock, latency, collision and reset contract.
