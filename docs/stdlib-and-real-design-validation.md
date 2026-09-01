# Standard library and real-design validation

This unnumbered product-validation slice expands reusable source libraries; it
does not start another numbered milestone or change the frozen formal
architecture.

The stable import namespace is `std`, while maintained source files live below
`stdlib/`. The resolver maps names such as `std.math.fixed` and
`std.bus.axi_stream` to ordinary `.zl` sources, resolves transitive imports in
dependency-first order, detects cycles, and includes dependency hashes in
BackendArtifact manifests.

First-class generic and concise fixed-point values use scaled integer storage.
`SF`/`UF` targets wrap on same-scale narrowing and `SF_Sat`/`UF_Sat` targets
saturate; arithmetic remains widened and exact. Fractional scale changes stay
explicit. Both Clash and direct-SystemVerilog use the same typed conversion IR;
neither backend defines numeric semantics.

The validation designs are:

- `StreamingPacketEngine`: AXI4-Lite to RegBus CSR control plus hierarchical
  AXI-Stream datapath, backpressure, packet boundaries, and state;
- `FixedPointPolyphaseFIR`: four eight-tap fixed-point phases implemented as
  reusable two-register child pipelines;
- `MultiChannelDMA`: two independent request/response engines, directional
  buffers, memory responders, AXI-Stream producers/sinks, and sibling state;
- `WishboneCsrTop`: Wishbone B4 Classic through the source-authored RegBus CSR
  target.

All four elaborate through backend-independent IR, emit direct SV accepted by
Verilator, and compile with the real Clash toolchain. In this original slice the
DMA deliberately used named sibling instances; later bounded instance-array
work added the exact supported combinations listed in the live
[syntax matrix](syntax-support-matrix.md). The emitted component definition is
shared per specialization while physical state remains per instance.

Yosys 0.68 `proc; opt; stat` on equivalent generated tops produced the following
technology-independent evidence. “FF” counts `$dff`/`$sdff` cells; these are not
vendor LUT/FF or Fmax claims because no common liberty/clock constraint was
provided.

| Top | Backend | Wires | Cells | FF | Mul | Add |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| StreamingPacketEngine | direct SV | 186 | 94 | 18 | 0 | 1 |
| StreamingPacketEngine | Clash | 188 | 109 | 16 | 0 | 1 |
| FixedPointPolyphaseFIR | direct SV | 100 | 68 | 8 | 32 | 28 |
| FixedPointPolyphaseFIR | Clash | 140 | 68 | 8 | 32 | 28 |
| MultiChannelDMA | direct SV | 291 | 148 | 16 | 0 | 18 |
| MultiChannelDMA | Clash | 257 | 178 | 14 | 0 | 10 |
| WishboneCsrTop | direct SV | 121 | 51 | 11 | 0 | 0 |
| WishboneCsrTop | Clash | 83 | 53 | 9 | 0 | 0 |

The deliberate limits at the time of this QoR table were full AXI4 bursts/IDs,
coefficient memories, CDC, AXI-Stream ID/dest/user, Wishbone burst/retry/lock,
and new formal observation families. Immutable compiler-generated synchronous
ROM is now supported and validated by the FFT work; writable/reloadable or
multiport coefficient memory remains deferred.

Original-slice validation: two independent `pytest -n 8 --dist=loadscope` runs passed
668/668 tests in 122.96 s and 124.51 s. Focused real-solver M35/M36/M38/M39
regression passed 20 tests. Real Clash generation, Verilator lint/simulation,
Yosys structural synthesis, Python compileall, and `git diff --check` pass.

## Current real-design continuation

The 668-test figure above is retained as provenance for this original stdlib
slice; it is not the current repository baseline. Subsequent ordinary ZLang
validation adds:

- the nine-stage [FFT512 SDF functional reference](../examples/fft/README.md#fft512-nine-stage-functional-reference),
  with compiler-generated twiddle ROM companions and complete direct-SV/Clash
  RTL replay;
- the complete canonical
  [802.11a transmitter project](80211a-transmitter-validation.md), covering the
  IEEE-authoritative 6/12/24-Mbit/s framer, scrambler, convolutional encoder,
  interleaver, mapper, inverse DIF-SDF IFFT64, natural-order reorder, cyclic
  prefix, stalls, and reset epochs without Wi-Fi-specific compiler paths;
- the bounded whole-vector IFFT64 numerical reference documented in the
  [802.11a validation report](80211a-transmitter-validation.md#ifft64-numerical-reference-elaboration-boundary),
  validated at N=64 through semantic/canonical/simulator paths and at N=8/N=16
  through both RTL backends.

The completion baseline for this original stdlib/real-design slice was **1849
passed with one explicit opt-in FFT512 replay skip**. That count is historical:
the replay is now routine and the current zero-skip release minimum is recorded
in [`release/status.json`](../release/status.json). The whole-vector N=64
IFFT remains a semantic/reference architecture rather than one required
4096-multiplier RTL top; the separately implemented streaming inverse DIF-SDF
hierarchy, packet framing/padding, and complete bounded transmitter composition
are validated by the canonical Wi-Fi project.
