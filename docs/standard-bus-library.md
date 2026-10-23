<a id="reference-standard-bus-library"></a>
# Standard buses


The production bus profiles are ordinary ZLang sources. The compiler contains
no AHB, AXI, APB, AXI-Stream, Wishbone, or RegBus transaction dispatcher.

| Import | Source-owned declarations | Initial profile |
| --- | --- | --- |
| `std.bus.reg` | `RegBus`, CSR bank/target | In-order request/response CSR boundary |
| `std.bus.axi_lite` | `AXI4Lite`, `AXI4LiteToRegBus` | 32/32-compatible, independent AW/W buffering |
| `std.bus.axi_burst` | `AXI4BurstSubset`, read/write views and helpers | No-ID, single-outstanding, full-width incrementing bursts |
| `std.bus.axi4` | Five-channel `AXI4`/`AXI4WithUser`, bounded read/write managers | IDs, bursts and optional USER payloads; under validation |
| `std.bus.axi4_subordinate` | Bounded read/write subordinate adapters | Backend transactions and responses; under validation |
| `std.bus.axi4_pins` | Flat-pin adapters | Source-owned five-channel pin projection |
| `std.bus.axi4_exclusive` | Reservation monitor and exclusive subordinate | Separate explicit same-edge commit contract; under validation |
| `std.bus.apb` | `APB`, `APBToRegBus` | APB setup/access sequencing |
| `std.bus.ahb_lite` | `AHBLite`, `AHBLiteToRegBus` | Single-manager, full-width AHB-Lite to RegBus |
| `std.bus.axi_stream` | `AXIStream`, `AXIStreamPipe` | Data/keep/strb/last ready/valid stream |
| `std.bus.wishbone` | `Wishbone`, `WishboneToRegBus` | B4 Classic single-beat, ack/err/stall |

Error completion is part of the source profile rather than a backend policy.
AXI4-Lite maps a failed RegBus write or read to `SLVERR` (`2'b10`) and holds the
complete B/R payload while the corresponding ready signal is low. APB forwards
the held RegBus error through `PSLVERR` on the access completion. Wishbone uses
mutually exclusive normal `ACK` and abnormal `ERR` termination; either one
retires the single outstanding request.

AHB-Lite preserves the protocol's pipelined address/data relationship. The
bridge captures an accepted address/control phase, consumes write data in the
following data phase, and holds `HREADYOUT` low while its one RegBus request is
pending. Local size/alignment errors and RegBus failures both use the standard
two-cycle ERROR response: low-ready/high-response followed by
high-ready/high-response. The first profile accepts aligned, full-bus-width
transfers for power-of-two byte-addressable data widths from 8 through 1024;
subword transfer strobes are deliberately deferred.

Reusable stateful cores in `std.bus.reg`, `std.bus.ahb_lite`,
`std.bus.axi_lite`, `std.bus.axi4`, `std.bus.axi4_subordinate`,
`std.bus.axi4_user_subordinate`, `std.bus.axi_burst`, `std.bus.axi_stream`,
`std.bus.apb`, and `std.bus.wishbone` do not impose a fixed physical reset
spelling. Each inherits the enclosing module's one exact clock/reset contract.
Native simulation, formal verification, and generated SystemVerilog all use
that same contract. Source-owned flat AXI pin adapters and concrete application
tops declare their physical domains explicitly rather than hiding reset policy
inside reusable cores.

The runnable AHB example uses conventional active-low `hresetn` with
asynchronous assertion and the language's two-edge synchronized release; the
same bridge also composes with a synchronous active-high parent. The APB,
AXI4-Lite, Wishbone, AXI-Stream, RegBus, and bounded AXI4 witnesses exercise
the same inheritance rule. Ambiguous multi-domain inheritance, a stateful core
compiled as a direct top without an explicit tooling-owned root domain, and
mismatched connected sequential logic fail closed. In every supported AHB
contract, `HREADYOUT` is high while reset is asserted.

`std` is a logical namespace mapped to the physical `stdlib/` source tree. Its
`.zhl` modules are discovered by convention. Cyclic imports and unsafe paths
are rejected.

The AXI4 source modules include IDs, bounded outstanding slots, five independent
channels, and optional USER sidebands. Directed and randomized tests cover
stalls, repeated IDs, slot reuse, error responses, native simulation against
generated Direct-SV, and direct-SystemVerilog generation. These checks do
**not** establish complete
external endpoint compliance: bounded formal endpoint proofs remain outstanding.
The ordinary subordinate never emits `EXOKAY`; exclusive commit requires the
separate `axi4_exclusive` contract and same-edge `exclusive_grant` gating.
The narrower `axi_burst` profile remains distinct and unchanged. AXI-Stream
ID/dest/user, Wishbone burst/retry, automatic adapters, and CDC are not added
by these profiles.

Runnable integrated examples are [AXI4-Lite CSR](../examples/axi_csr_top.zhl),
[APB CSR](../examples/apb_csr_top.zhl),
[AHB-Lite CSR](../examples/ahb_csr_top.zhl),
[Wishbone CSR](../examples/wishbone_csr_top.zhl), and the
[streaming packet engine](../examples/streaming_packet_engine.zhl). The bounded
burst helpers are exposed by the
[ZTPU AXI burst witness](../examples/ztpu_axi_burst.zhl). Python models in
`tests/support/standard_bus.py` are independent test oracles, not compiler
runtime implementations.

The AHB-Lite contract follows the
[Arm AMBA 3 AHB-Lite protocol](https://documentation-service.arm.com/static/5f914801f86e16515cdc2a27)
rather than the
older ZTPU model's simplified same-cycle "AHB-like" behavior. In particular,
write address/control and `HWDATA` are not sampled in the same phase, and ERROR
is not shortened to one cycle. The source profile and executable tests freeze
that bounded contract; broader AHB features remain explicit exclusions below.

<a id="reference-standard-bus-library-bounded-ztpu-axi-burst-subset"></a>
## Bounded ZTPU AXI burst subset

`std.bus.axi_burst` is ordinary source-authoritative ZLang HDL. It defines a
combined `AXI4BurstSubset<AW,DW>` with independent AR/R and AW/W/B ready/valid
channels, together with read-only and write-only protocol views for ZTPU's
separate physical master ports. Address payloads carry byte address, eight-bit
beats-minus-one `len`, and three-bit beat `size`; read and write data carry
`last`, and R/B carry the exact two-bit response value.

The reusable `AXI4BurstReader<AW,DW,LW>` and
`AXI4BurstWriter<AW,DW,LW>` accept aligned 1-256-beat requests and permit one
outstanding transaction each. AR and AW payloads are snapshotted and remain
stable until accepted. Read consumer backpressure drives RREADY directly. The
writer accepts AW before exposing W, while AW, W, and B remain independent
handshakes; its ready/valid producer retains W data while stalled. Counted
framing owns completion: the reader reports early or missing RLAST but drains
the requested beat count, while the writer generates WLAST on its locally
counted final beat. Any nonzero RRESP or BRESP sets a sticky boolean error for
that transaction epoch.

The concrete `AW=64,DW=32` reader/writer witness passes semantic and canonical
round trips, deterministic artifact checks, bounded simulator traces, and
Direct-SV/Verilator. Validation
includes 1- and 256-beat requests, invalid 0
and 257 lengths, misalignment, independent channel stalls, stable owned
payloads, RLAST/RRESP/BRESP failures, reset in active phases, and ignored starts
while busy. No compiler, simulator, or backend dispatches on AXI names.

This profile deliberately omits IDs, WSTRB, burst-kind, lock, cache, protection,
QoS, region and user fields, multiple outstanding transactions, UB-DMA burst
chunking, command-descriptor decoding, fences, CDC, or implicit adaptation. Its
accepted semantic boundary is the bounded profile stated above.

## AXI4-Lite, APB, and RegBus

`std.bus.reg`, `std.bus.axi_lite`, and `std.bus.apb` are ordinary imported
library sources. The compiler checks structural protocol roles, typed
ready/valid members, ownership, and domains; transaction state machines belong
to library modules. `RegBus<32,32>` is the typed CSR boundary.

The AXI4-Lite and APB profiles use one clock/reset and one outstanding
operation. `Axi4LiteToRegBus` buffers AW and W independently and joins them
after both transfers; AR returns one held R response. `ApbToRegBus` implements
setup/access phases and holds controls stable while waiting for PREADY.
Top-level aggregate endpoints expose typed public leaves (for example,
`axi_aw_valid` and `apb_psel`); `connect axi -> frontend.axi` delegates a
same-role endpoint to a child.

`RegBusCSRTarget` is ordinary source-authored state with RW, sticky W1C, and
one-cycle pulse registers. Its response data and valid bit are held until a
response transfer. Formal attachment requires exact published observations:
properties needing hidden or unsupported state are explicitly skipped or
rejected under the selected policy. The AXI4-Lite/APB/RegBus profiles do not
provide full AXI4 behavior. The separate `axi_burst` and `axi4` modules have their
own bounded contracts and qualification limits, described above.
