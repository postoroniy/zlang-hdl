<a id="reference-stdlib-buses"></a>
## Standard library and buses

The standard library is ordinary tracked ZLang source under the logical `std`
namespace. The compiler checks types, protocol roles, ownership, and domains;
library modules own their transaction state machines. No bus name activates a
hidden compiler dispatcher.

### Streams, storage, math, and coding

| Import | Main facilities |
| --- | --- |
| `std.math.fixed` | Exact fixed-point abs, min/max, clamp, MAC, and explicit quantization helpers |
| `std.math.complex` | Generic `Complex<T>`, butterfly, operators, and explicit complex quantization |
| `std.stream.core` | Ready/valid register slice, skid buffer, FIFO, mux, and demux |
| `std.stream.serialization` | Bounded vector serializer and raw-bit collector |
| `std.dsp.fft` | Exact butterfly, bit reversal, and compile-time twiddle generation |
| `std.storage.core` | Delay, FIFO, reorder, ping-pong, memory, async-memory, async-FIFO, and ROM wrappers |
| `std.coding.core` | Parity, bit reversal, checked LFSR, and convolution helpers |

These declarations retain normal language semantics. Complex multiplication
keeps full product width and scale; callers place quantization explicitly.
Stream routing propagates backpressure only through the selected static path.
Storage wrappers lower to the memory contracts in Chapter 9 and do not grant
implicit CDC or target mapping.

### Bus profiles

| Import | Supported profile |
| --- | --- |
| `std.bus.reg` | In-order RegBus request/response and CSR target |
| `std.bus.axi_lite` | AXI4-Lite with independent AW/W buffering |
| `std.bus.apb` | APB setup/access sequencing |
| `std.bus.ahb_lite` | Single-manager, aligned full-width AHB-Lite to RegBus |
| `std.bus.wishbone` | Wishbone B4 Classic single-beat with ACK/ERR/STALL |
| `std.bus.axi_stream` | Data/keep/strb/last ready/valid stream |
| `std.bus.axi_burst` | No-ID, single-outstanding, full-width incrementing bursts |
| `std.bus.axi4` | Bounded five-channel AXI4 with IDs and optional USER payloads |

Reusable stateful bus cores inherit one exact enclosing clock/reset contract.
Concrete application tops and flat-pin adapters own physical pins. Ambiguous
multi-domain inheritance, missing direct-top ownership, and incompatible
connected state owners are errors.

### CSR and RegBus

`RegBus<32,32>` is the common typed CSR boundary. `RegBusCSRTarget` provides
RW, sticky W1C, and one-cycle pulse fields; responses remain stable until
transfer. AXI4-Lite, APB, AHB-Lite, and Wishbone frontends translate their
bounded profiles to RegBus without changing error completion semantics.

### AXI4-Lite, APB, AHB-Lite, and Wishbone

AXI4-Lite buffers AW and W independently and joins them only after both
transfers. APB implements setup/access phases and holds controls while waiting
for `PREADY`. Wishbone uses mutually exclusive normal `ACK` and abnormal `ERR`
termination.

AHB-Lite preserves the pipelined address/data relationship: an accepted
address/control phase is followed by write data in the next data phase.
`HREADYOUT` remains low while the RegBus operation is pending. Local geometry
errors and RegBus failures use the standard two-cycle ERROR response. The
current profile accepts aligned, full-width transfers for power-of-two
byte-addressable widths from 8 through 1024; subword transfers are outside the
profile.

### AXI-Stream

`AXIStream<DW>` carries byte-lane `data`, `keep`, `strb`, and `last`.
`AXIStreamOf<T>` transports one typed semantic payload per transfer; it does
not define an external TDATA serialization for every aggregate. Register-slice,
skid-buffer, FIFO, mux, and demux components preserve payload stability under
stall.

### Bounded burst and AXI4 profiles

`AXI4BurstReader` and `AXI4BurstWriter` permit one aligned 1–256 beat
transaction. AR/AW payloads are snapshotted until accepted, channel
backpressure is independent, and counted framing checks RLAST or generates
WLAST. Nonzero RRESP/BRESP sets the transaction's error result. The profile
omits IDs, WSTRB, burst kind, cache/protection/QoS/region/user fields, multiple
outstanding transactions, fences, CDC, and implicit adaptation.

The broader `std.bus.axi4` profile supplies five channels, bounded outstanding
slots, IDs, bursts, and optional USER fields. It remains a bounded library
profile rather than a claim of complete support for every AXI4 endpoint and
interconnect feature. Exclusive commit uses its separate reservation contract;
the ordinary subordinate does not emit `EXOKAY`.

### Imports and current boundaries

Imports resolve transitively from compiler-shipped sources and pinned project
dependencies; cycles and unsafe paths are rejected. Ordinary compilation does
not fetch dependencies. Automatic protocol adapters, implicit CDC, AXI-Stream
ID/dest/user, Wishbone burst/retry, AHB arbitration/SPLIT/RETRY, and arbitrary
full AXI4 topology are not added by the library.

Runnable integrations include
[AXI4-Lite CSR](../../examples/axi_csr_top.zhl),
[APB CSR](../../examples/apb_csr_top.zhl),
[AHB-Lite CSR](../../examples/ahb_csr_top.zhl),
[Wishbone CSR](../../examples/wishbone_csr_top.zhl), and the
[streaming packet engine](../../examples/streaming_packet_engine.zhl).
