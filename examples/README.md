# ZLang HDL examples

The examples are executable compiler contracts, not fragments of one large
demo.  A top-level `.zhl` file normally stays standalone so that one command
can isolate one language, implementation, protocol, or backend behavior:

```bash
.venv/bin/zlang examples/add.zhl --check
.venv/bin/zlang examples/add.zhl --systemverilog build/Add.sv
```

Multi-file designs belong under `projects/` and own a `zlang.toml`, lockfile,
and local README.  Do not merge unrelated standalone witnesses merely to
reduce the file count: their small compile roots, diagnostics, formal goals,
and regression identities are useful boundaries.

## Preferred source style

Examples use the concise form when it preserves the point being taught:

- infer local expression types unless an annotation is the subject of the
  example;
- write carry-growing arithmetic directly when its inferred result already
  matches the destination (`u8 + u8` is `u9`); do not extend operands merely
  to restate the operator's exact type;
- use contextual `extend(value)`/`truncate(value)` at an explicitly typed
  output, state update, field, argument, or return boundary; keep `<N>` when
  the conversion has no such context or the width itself is being taught;
- use struct field punning (`Field { value }`) when the source name matches;
- use concise child declarations and binding punning (`lane : Lane { input }`)
  when unambiguous; retain `inst` when it disambiguates a resource-like name or
  a name such as `fifo` or `memory` is also a declaration keyword;
- use `value -> endpoint` for direct aggregate endpoint wiring and `connect`
  when buffering, adaptation, or CDC policy is part of the example;
- use `generate` for bounded compile-time structure, never for runtime work;
- use `<-` only for state updates and `drive` for transient rule/FSM outputs.

Some files intentionally retain a more explicit form.  Bus and protocol
examples spell out endpoint fields to show ownership; comparison witnesses
keep separate modules so their implementation constraints remain visible; and
`all_syntax.zhl` deliberately exercises both concise and explicit grammar.
Comments should explain those choices rather than restating each expression.

## Standalone examples

Stream composition/DSP introductions:

- [`framed_crc.zhl`](framed_crc.zhl): broadcast a framed byte stream to a mirror
  and CRC-32 engine; [ready-to-run events](framed_crc.events.jsonl).
- [`stream_fir8.zhl`](stream_fir8.zhl): immutable-coefficient parallel FIR with
  one final nearest-even/saturating conversion; [impulse events](stream_fir8.events.jsonl).
- [`farrow.zhl`](farrow.zhl): cubic fractional delay with per-sample μ and
  final-only output rounding; [events](farrow.events.jsonl) and
  [numerical/pipeline contract](../docs/stdlib-farrow.md).
- [`fir_reload.zhl`](fir_reload.zhl): candidate parallel FIR with inactive-bank
  loading and an explicit commit after result retirement;
  [events](fir_reload.events.jsonl) and
  [bank/handshake contract](../docs/stdlib-reloadable-fir.md).
- [`fir_folded.zhl`](fir_folded.zhl): explicitly sequential three-tap FIR,
  latency/II=3, with [raw fixed-point events](fir_folded.events.jsonl).
- [`fir_interpolate.zhl`](fir_interpolate.zhl): parallel phase sums with two
  ordered outputs per accepted input; [events](fir_interpolate.events.jsonl).
- [`cic_interpolate.zhl`](cic_interpolate.zhl): modular ×3 CIC interpolation,
  explicit state width and no gain compensation; [events](cic_interpolate.events.jsonl).
- [`halfband.zhl`](halfband.zhl): exact ×2 decimation and interpolation with
  checked symmetric coefficients; [events](halfband.events.jsonl).
- [`nco_linear.zhl`](nco_linear.zhl): two-edge fractional ROM interpolation;
  the existing [NCO events](nco.events.jsonl) also supply its phase increments.

See [commands and numerical/stream contracts](../docs/stdlib-stream-dsp.md).

### Oscillators, multirate filters and coding

- [`cordic.zhl`](cordic.zhl): rotation-only signed-turn sin/cos pipeline;
  [`cordic.events.jsonl`](cordic.events.jsonl) requests five angles and drains it.
- [`nco.zhl`](nco.zhl): accepted-sample phase accumulator with paired ROM
  sine/cosine; [`events`](nco.events.jsonl).
- [`multirate_filters.zhl`](multirate_filters.zhl): explicit-width CIC and
  exact-accumulating polyphase FIR decimators; [`events`](multirate_filters.events.jsonl).
- [`coding_blocks.zhl`](coding_blocks.zhl): extracted 24-bit scrambler and K=7
  convolutional encoder; [`scrambler events`](scrambler24.events.jsonl) and
  [`encoder events`](convolutional24.events.jsonl).
- [`secded.zhl`](secded.zhl): 32-bit extended-Hamming encoding/decoding,
  [single/double-error events](secded32.events.jsonl) and
  [layout/status contract](../docs/stdlib-secded.md).
- [`rs_dvb.zhl`](rs_dvb.zhl): framed DVB RS(204,188) encoding;
  [events](rs_dvb.events.jsonl) and [byte/framing contract](../docs/stdlib-rs-dvb.md).
- [`rs_dvb_decode.zhl`](rs_dvb_decode.zhl): correct two corrupted symbols and
  recover the188-byte ramp; [events](rs_dvb_decode.events.jsonl) and
  [decoder/cycle contract](../docs/stdlib-rs-dvb.md#decoder).
- [`viterbi.zhl`](viterbi.zhl): framed K=7 hard-decision decoding;
  [events](viterbi.events.jsonl) and [soft/traceback/word contract](../docs/stdlib-viterbi.md).

See [complete commands and sample/bit-order contracts](../docs/stdlib-multirate-coding.md).

### Language, values, and basic hardware

| Source | Purpose |
| --- | --- |
| [`add.zhl`](add.zhl) | Minimal combinational module and sized arithmetic. |
| [`alu.zhl`](alu.zhl) | `switch`-selected arithmetic and bitwise operations. |
| [`all_syntax.zhl`](all_syntax.zhl) | Executable syntax conformance tour; intentionally broad and explicit. |
| [`counter.zhl`](counter.zhl) | Small synchronous state update. |
| [`extended_add.zhl`](extended_add.zhl) | Explicit width growth and a named result type. |
| [`metadata_datapath.zhl`](metadata_datapath.zhl) | Aggregate metadata carried with a datapath value. |
| [`tagged_union.zhl`](tagged_union.zhl) | Nominal tagged data, matching, and frozen packing. |

### Rules, state, pipelines, and hierarchy

| Source | Purpose |
| --- | --- |
| [`contracted_add.zhl`](contracted_add.zhl) | Source-level contract attached to simple arithmetic. |
| [`delayed_mul.zhl`](delayed_mul.zhl) | Explicit delay semantics. |
| [`elastic_pipeline_auto.zhl`](elastic_pipeline_auto.zhl) | Automatically partitioned elastic pipeline. |
| [`fft_sdf_stage_atomic_transition.zhl`](fft_sdf_stage_atomic_transition.zhl) | Atomic FIFO/state transition shaped like one SDF stage. |
| [`general_expression_pipeline.zhl`](general_expression_pipeline.zhl) | Pipeline partitioning of an uneven expression DAG. |
| [`generated_reduce.zhl`](generated_reduce.zhl) | Bounded compile-time generation and reduction. |
| [`hierarchy_composition.zhl`](hierarchy_composition.zhl) | Direct child composition with aggregate values. |
| [`indexed_instance_array.zhl`](indexed_instance_array.zhl) | Compile-time instance array and indexed wiring. |
| [`logic_state_register.zhl`](logic_state_register.zhl) | Native `0/1/U/X` register behavior. |
| [`mapped_sum.zhl`](mapped_sum.zhl) | Mapped vector operation and reduction. |
| [`multi_clock_stateful.zhl`](multi_clock_stateful.zhl) | Explicit domains and legal crossings. |
| [`pipelined_mac.zhl`](pipelined_mac.zhl) | Fixed-latency multiply-accumulate pipeline. |
| [`registered_output.zhl`](registered_output.zhl) | `out reg`, state update, and transient `drive`. |
| [`rule_action.zhl`](rule_action.zhl) | Atomic state and transient-output rule effects. |
| [`rule_counter.zhl`](rule_counter.zhl) | Guards, priorities, and counter updates. |
| [`rule_local_memory.zhl`](rule_local_memory.zhl) | Rules owning memory read/write effects. |
| [`sequential_instance_array.zhl`](sequential_instance_array.zhl) | Stateful instance-array composition. |
| [`storage_instance_array.zhl`](storage_instance_array.zhl) | FIFO, memory, and ROM instance ownership. |

### Exact math and implementation selection

| Source | Purpose |
| --- | --- |
| [`complex_fft_butterfly.zhl`](complex_fft_butterfly.zhl) | Typed complex fixed-point butterfly. |
| [`fft/complex_multiply_three_dsp.zhl`](fft/complex_multiply_three_dsp.zhl) | The same complex `a*b` selects three or four DSPs through `implement` intent and bounded bilinear exploration. |
| [`cost_mac.zhl`](cost_mac.zhl) | MAC candidate costs with DSP use available. |
| [`cost_mac_no_dsp.zhl`](cost_mac_no_dsp.zhl) | The same candidate surface with DSP forbidden. |
| [`dot_builtin.zhl`](dot_builtin.zhl) | Built-in dot-product expression. |
| [`dot_product.zhl`](dot_product.zhl) | Explicit dot-product structure. |
| [`dot_product_pipelined.zhl`](dot_product_pipelined.zhl) | Pipelined eight-term dot product. |
| [`dot_product_pipelined_12.zhl`](dot_product_pipelined_12.zhl) | Twelve-cycle comparison point for the same value function. |
| [`fir2.zhl`](fir2.zhl) | Tiny generic FIR/tap composition. |
| [`fixed_fir_architectures.zhl`](fixed_fir_architectures.zhl) | Deliberately separate manual FIR architectures. |
| [`fixed_polyphase_fir.zhl`](fixed_polyphase_fir.zhl) | Fixed-point polyphase hierarchy. |
| [`implementation_intent.zhl`](implementation_intent.zhl) | Canonical bounded `implement` intent witnesses. |
| [`mac_choice.zhl`](mac_choice.zhl) | Alternative MAC implementations under one intent. |
| [`shift_multiply.zhl`](shift_multiply.zhl) | Exact shift/multiply optimization witness. |
| [`symmetric_fixed_fir.zhl`](symmetric_fixed_fir.zhl) | Functional symmetric FIR value expression. |
| [`symmetric_fixed_fir_auto.zhl`](symmetric_fixed_fir_auto.zhl) | Target-aware automatic FIR implementation. |
| [`symmetric_fixed_fir_dsp_pipelines.zhl`](symmetric_fixed_fir_dsp_pipelines.zhl) | Explicit DSP pipeline comparison. |
| [`target_bram_memory.zhl`](target_bram_memory.zhl) | Generic memory semantics with an optional target mapping. |
| [`temporal_shared_multiply.zhl`](temporal_shared_multiply.zhl) | Narrow capacity-one `a*b+c*d` temporal sharing. |

### Protocols, storage, and transactions

| Source | Purpose |
| --- | --- |
| [`cdc_async_fifo.zhl`](cdc_async_fifo.zhl) | Ready/valid asynchronous FIFO crossing. |
| [`cdc_handshake.zhl`](cdc_handshake.zhl) | Handshake CDC policy. |
| [`cdc_level.zhl`](cdc_level.zhl) | Synchronized level crossing. |
| [`cdc_pulse.zhl`](cdc_pulse.zhl) | Pulse-toggle crossing. |
| [`credit_source.zhl`](credit_source.zhl) | Credit-based source behavior. |
| [`credit_to_rv.zhl`](credit_to_rv.zhl) | Credit-to-ready/valid adapter. |
| [`fifo_bridge.zhl`](fifo_bridge.zhl) | FIFO-backed protocol bridge. |
| [`hierarchical_protocol.zhl`](hierarchical_protocol.zhl) | Buffered ready/valid hierarchy. |
| [`hierarchical_request_response.zhl`](hierarchical_request_response.zhl) | Request/response composition and roles. |
| [`multichannel_dma.zhl`](multichannel_dma.zhl) | Two DMA channels with request/response and AXI Stream. |
| [`packet_data.zhl`](packet_data.zhl) | Packet payload declaration and flow. |
| [`packet_fixed_arbiter.zhl`](packet_fixed_arbiter.zhl) | Fixed-priority packet arbitration. |
| [`packet_round_robin.zhl`](packet_round_robin.zhl) | Round-robin packet arbitration. |
| [`request_client.zhl`](request_client.zhl) | Stateful request/response client. |
| [`rv_buffer.zhl`](rv_buffer.zhl) | Buffered ready/valid connection. |
| [`rv_connect.zhl`](rv_connect.zhl) | Direct ready/valid connection. |
| [`rv_fifo_instance_array.zhl`](rv_fifo_instance_array.zhl) | Independent ready/valid array lanes. |
| [`rv_passthrough.zhl`](rv_passthrough.zhl) | Minimal ready/valid endpoint pass-through. |
| [`rv_to_credit.zhl`](rv_to_credit.zhl) | Ready/valid-to-credit adapter. |
| [`simple_dma.zhl`](simple_dma.zhl) | Small hierarchical DMA/request-response design. |
| [`streaming_packet_engine.zhl`](streaming_packet_engine.zhl) | CSR-controlled AXI Stream engine. |
| [`sync_memory.zhl`](sync_memory.zhl) | Synchronous inferred memory. |
| [`vc_credit_source.zhl`](vc_credit_source.zhl) | Virtual-channel credit source. |

### CSRs and source-authored buses

| Source | Purpose |
| --- | --- |
| [`ahb_csr_top.zhl`](ahb_csr_top.zhl) | AHB-Lite-to-RegBus/CSR composition. |
| [`apb_csr_top.zhl`](apb_csr_top.zhl) | APB-to-RegBus/CSR composition. |
| [`axi_csr_top.zhl`](axi_csr_top.zhl) | AXI4-Lite-to-RegBus/CSR composition. |
| [`axi_single_beat.zhl`](axi_single_beat.zhl) | Bounded source-owned single-beat AXI subset. |
| [`control_csr.zhl`](control_csr.zhl) | Compact CSR declaration and generated views. |
| [`engine_csr.zhl`](engine_csr.zhl) | Read-only, writable, W1C, and sticky CSR fields. |
| [`wishbone_csr_top.zhl`](wishbone_csr_top.zhl) | Wishbone-to-RegBus/CSR composition. |
| [`ztpu_async_memory.zhl`](ztpu_async_memory.zhl) | Inferred asynchronous-read memory witness. |
| [`ztpu_axi_burst.zhl`](ztpu_axi_burst.zhl) | Runnable source-authored AXI burst library witnesses. |
| [`ztpu_banked_memory.zhl`](ztpu_banked_memory.zhl) | Replicated-read banked memory. |

## Focused suites and projects

- [`fft/`](fft/) contains independent numerical and auto-pipeline FFT roots;
  see its [README](fft/README.md).
- [`structural/`](structural/) contains one bounded structural stress shape per
  file; see its [README](structural/README.md).  `structural/test.zhl` is an
  internal large-emission witness and is not part of the public example set.
- [`verification/`](verification/) contains one formal behavior per root; see
  its [README](verification/README.md).
- [`projects/80211a_transmitter/`](projects/80211a_transmitter/) is the
  consolidated multi-file example project with manifest, lockfile, simulation,
  formal goals, and its own [README](projects/80211a_transmitter/README.md).

The `projects/80211ad_*` trees are intentionally outside this review and are
not part of the normal public example corpus.

## Generated artifacts

[`generated/`](generated/) stores reviewed output examples and reports for
selected roots. They are evidence, not input source units; its
[catalog](generated/README.md) identifies each artifact family and owner.
