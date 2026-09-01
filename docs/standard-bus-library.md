# Standard bus library

The production bus profiles are ordinary ZLang sources. The compiler contains
no AXI, APB, AXI-Stream, Wishbone, or RegBus transaction dispatcher.

| Import | Source-owned declarations | Initial profile |
| --- | --- | --- |
| `std.bus.reg` | `RegBus`, CSR bank/target | In-order request/response CSR boundary |
| `std.bus.axi_lite` | `AXI4Lite`, `AXI4LiteToRegBus` | 32/32-compatible, independent AW/W buffering |
| `std.bus.apb` | `APB`, `APBToRegBus` | APB setup/access sequencing |
| `std.bus.axi_stream` | `AXIStream`, `AXIStreamPipe` | Data/keep/strb/last ready/valid stream |
| `std.bus.wishbone` | `Wishbone`, `WishboneToRegBus` | B4 Classic single-beat, ack/err/stall |

Error completion is part of the source profile rather than a backend policy.
AXI4-Lite maps a failed RegBus write or read to `SLVERR` (`2'b10`) and holds the
complete B/R payload while the corresponding ready signal is low. APB forwards
the held RegBus error through `PSLVERR` on the access completion. Wishbone uses
mutually exclusive normal `ACK` and abnormal `ERR` termination; either one
retires the single outstanding request.

`std` is a logical namespace mapped to the physical `stdlib/` source tree. The
resolver discovers `.zl` modules by convention, resolves a deterministic
dependency closure, rejects cycles and unsafe paths, and records every logical
identity/content hash in semantic, canonical, and backend artifacts.

Full AXI4 bursts/IDs, AXI-Stream sideband ID/dest/user, Wishbone burst/retry,
automatic adapters and CDC are deliberately not provided by these profiles.

Runnable integrated examples are [AXI4-Lite CSR](../examples/axi_csr_top.zl),
[APB CSR](../examples/apb_csr_top.zl),
[Wishbone CSR](../examples/wishbone_csr_top.zl), and the
[streaming packet engine](../examples/streaming_packet_engine.zl). Python models in
`zlang/standard_bus.py` remain independent test oracles only.

## Historical migration record

The remainder of this section records the order in which the source-authoritative
bus boundary was reached. Statements such as “out of scope” below describe the
named historical slice, not the current support table above. Current project
imports and backend coverage are documented in
[Projects and dependencies](projects-dependencies.md) and
[Direct SystemVerilog](direct-systemverilog.md).

The generic public boundary for source-defined bus interfaces uses role-derived
leaf directions, deterministic external names, the flat Clash
wrapper around the closed component ABI, manifest metadata, formal ownership,
legacy-test migration, and the later implementation exit criteria. No compiler
or backend implementation is included in that review.

The implementation now projects explicitly declared top-level aggregate
interfaces through that generic ABI. AXI and APB use ordinary source modules;
their public names are generic paths (`axi_aw_valid`, `apb_psel`, and so on),
not compiler-owned bus pin tables. Top-to-child exposure is an explicit
same-role delegation (`connect axi -> frontend.axi`), while child-to-child
connections retain complementary protocol roles.

That projection is now the only public ABI for both RTL backends. Struct-valued
payloads are exposed as separate named fields such as
`axi_w_payload_data` and `axi_w_payload_strb`; vector-valued leaves remain one
native unpacked array port rather than a packed bus or one pin per element.
Clash uses a generated typed SystemVerilog boundary around its packed core, and
direct SystemVerilog uses the same `TopPhysicalABI` packing contract. Neither
path contains an AXI/APB member-name table.

The generic prerequisite for this migration is the semantic model for
parameterized structs/protocols, role-qualified
aggregate endpoints, member ownership, connection expansion, hierarchy,
backend ABI, manifests, and formal integration. No implementation is included
in this review.

At that point the compiler shipped a narrow source resolver for `std.bus.reg`,
`std.bus.axi_lite`, and `std.bus.apb`. The source files now live at
`stdlib/bus/reg.zl`, `stdlib/bus/axi_lite.zl`, and `stdlib/bus/apb.zl`.
Source-level filesystem/revision syntax remained unsupported. Imported source
modules received a stable logical identity and SHA-256 content hash; later
work added the separate pinned path/Git project resolver.

The compiler understands generic structural protocol schemas (roles, typed
ready/valid members, ownership, and optional domains). AXI-Lite and APB
channel structure, buffering, phase machines, and contracts belong to normal
library components. `RegBus<32,32>` is the backend-independent CSR boundary.

The first profiles use one clock/reset and one outstanding operation.
`Axi4LiteToRegBus` independently buffers AW and W and joins them only after
both transfers; AR returns one held R response. `ApbToRegBus` implements
explicit setup/access behavior and holds controls stable while waiting for
PREADY. Safety families reuse M35 property generation.

For that slice, full AXI4, bursts, IDs, AXI-Stream, Wishbone, CDC, adapters, and
DMA bus conversion were excluded. SimpleDMA remained on generic
request/response.

The executable backend-independent reference models are in
`zlang/standard_bus.py`. They use explicit state transitions and are suitable
inputs to the closed bundled Clash ABI. The compiler publishes the generic
schema and import identity; bus behavior remains library-owned rather than
compiler-special.

The former hard-coded Clash component emitter has been removed. There is no
AXI/APB production dispatch in the compiler or backend. Production authority
is the `.zl` module elaborated through the generic semantic and Clash paths;
`zlang/standard_bus.py` remains only as an independent Python behavior oracle.

Clash emitted a reusable `topEntity` wrapper for each component, and Verilator
lint has been exercised on all three generated designs. The next integration
step is elaborating these library components as same-unit children around a
CSR target. Direct SystemVerilog was out of scope for that historical step.

Yosys formal front-end preparation also succeeds for each generated RTL
module. The library formal attachment is ready for a source-level child
hierarchy; runtime trace simulation remains covered by the backend-independent
step models until the CSR target is connected through ZLang elaboration.

Source-level examples remain in `examples/axi_csr_top.zl` and
`examples/apb_csr_top.zl`. Their imports elaborate ordinary source modules and
both now compile through the generic Clash hierarchy to Verilog. Parameterized
aggregate endpoint syntax and multi-channel stateful lowering are exercised by
these examples without a bus-specific emitter branch.

The Python models in `zlang/standard_bus.py` remain independent verification
oracles and are intentionally retained.

The generic parameterized aggregate endpoint slice is now available for future
stdlib refinement. It elaborates role-qualified members and exact parameter
widths from ordinary `.zl` source; it does not add bus transaction behavior.

Migration checkpoint: `reg.zl`, `axi_lite.zl`, and `apb.zl` contain the
authoritative parameterized source protocols and state machines. The generic
`TopAggregateABI` now projects explicitly declared top aggregates into flat
external ports while retaining the closed bundled child ABI internally. Same-
role top-to-child delegation is explicit in elaborated IR, and child-to-child
connections retain their complementary handshake semantics. No AXI/APB logic is
special-cased in the compiler or Clash backend.

Validation at this checkpoint: the focused source/aggregate suite passes,
including the migrated AXI/APB hierarchy tests and ABI round-trip tests;
`examples/axi_csr_top.zl` and `examples/apb_csr_top.zl` compile with real Clash
1.11 and lint cleanly with Verilator 5.044. Top aggregate manifests use version
3 with structured member paths and v2 manifests remain readable. Arrays,
partial exposure, unsupported protocol kinds, and direct-SV top aggregate
promotion remain intentionally out of scope.

The source `RegBusCSRTarget` now includes a small real CSR fixture used by both
frontends: an RW location at offset 0, a sticky W1C location at offset 4, and a
one-cycle pulse location at offset 8. The target is ordinary ZLang state and
rules, so generic hierarchy emits the state rather than calling a bus-specific
model. The frozen CSR model remains the semantic oracle for reset, sticky
precedence, W1C clearing, and pulse duration. The source hierarchy and
generated RTL are covered by existing transaction/reset checks, while
failure-first SBY mutation classification is covered by the formal integration
suite. The later generic recursive formal instrumentation now validates the
published RW/W1C/pulse CSR state through generated AXI4-Lite and APB formal tops;
all nine semantic leaves are connected without bus-name dispatch. Its generic,
bus-independent architecture is documented in
[compositional-formal-binding.md](compositional-formal-binding.md) and the
current [compositional formal-binding guide](compositional-formal-binding.md).
Properties requiring an unpublished hidden observation still skip explicitly.
Liveness, broader recursive observation families, and full AXI remain excluded.
