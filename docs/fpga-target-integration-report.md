<a id="gowinecp5-a24-integration-status"></a>
# Gowin and Lattice qualification reference

This reference identifies the devices, board revisions and design configurations
covered by the a24 FPGA evidence. For commands and pin/clock contracts, use the
[build guide](fpga-builds.md); for admitted arithmetic and memory shapes, use the
[architecture profiles](fpga-architecture-profiles.md).

<a id="current-qualification"></a>
## Devices, boards and completed software flows

| Manufacturer / device | Board or fixture | Designs | Completed level |
| --- | --- | --- | --- |
| Gowin GW1NR9 C7/I6 | BRS-100-GW1NR9 rev1.0 | five portable designs; 35 architecture configurations | RTL validation, synthesis, routing and bitstream packing |
| Gowin GW1NR9 C7/I6 | EPM11 rev1.0 | five portable designs | RTL validation, synthesis, routing and bitstream packing |
| Gowin GW1NR9 C6/I5 | `gw1nr9-qn88p-c6i5-reference` device pin fixture | five portable designs | RTL validation, synthesis, routing and bitstream packing; device fixture, not a populated-board claim |
| Lattice ECP5-25F | OrangeCrab r0.2 | five portable designs; 35 architecture configurations | RTL validation, synthesis, routing and bitstream packing |
| Lattice ECP5-25F | OrangeCrab r0.2.1 | five portable designs | RTL validation, synthesis, routing and bitstream packing |
| Lattice ECP5-85F | OrangeCrab r0.2.1 | five portable designs; 35 architecture configurations | RTL validation, synthesis, routing and bitstream packing |

The [machine-readable inventory](../release/fpga-qualification.json) binds each
execution to exact source, device, board/constraint, RTL, tool/library/chipdb and
bitstream identities. It contains 30 portable combinations and 105 architecture
configurations: 87 arithmetic and 18 memory/FIFO builds. The five portable designs
are counter, GPIO, fixed-point multiplication, synchronous RAM and hierarchy.

The software flow uses pinned OSS CAD Suite 2026-10-21: Yosys 0.70+186,
nextpnr 0.11.1-77-g6b438a1c, Trellis ecppack 1.4-86-ga31ff70 and Apicula
gowin_pack. A build authenticates the complete tool/library/device-database
inventory. The separate Gowin EDA interface exports a relocatable batch project;
see [vendor project export](fpga-builds.md#programming-and-unsupported-capabilities).

<a id="architecture-parity-qualification-scope"></a>
<a id="recorded-specialization-boundaries"></a>
## RTL and formal domains

| Class | Actual-RTL proof domain | Routed architecture fixture |
| --- | --- | --- |
| Multiply | full signed/unsigned 18×18 | latency 1/2/3 |
| MAC / preadd | signed 18×18 + signed 53; exact (signed 17 + signed 17) × signed 18 | latency 1/2/3 |
| Ordered reductions | two signed 18×18 products with subtraction; four signed 8×8 products | latency 1/2/3 |
| Parallel products | six signed 4-bit inputs, two complete result fields, 1…8 unique products | latency 1, 1…8 hard multipliers |
| Symmetric FIR | eight samples/four coefficients, `fixed<12,10>`, final nearest-even/saturate `fixed<16,14>` | latency 1/2/3, four hard multipliers plus fabric |
| RAM | independent 4×8 scoreboard for admitted collision/port policies | 256×16, one block |
| Async FIFO | independent depth-4, 2-bit queue, stalls and independent/paused clocks with coordinated reset | depth 4 × 8, one block |

The recorded arithmetic proofs use GW1NR9 C6/I5 and Lattice ECP5-25F target
specializations; the physical fixture matrix above is recorded separately.
These are specialization-specific unbounded RTL proofs, not universal proofs of
all legal widths, depths or device modes. The FIFO checks include active
read/write address separation. Digital clock proofs require coordinated reset
epochs and establish neither metastability behavior nor physical reset margins.

Actual-RTL checks use independent arithmetic/queue references, connected
conversion lemmas and counterexample-required mutations. The positive recipes
retain depth 8 and a 30-second deadline. Required proof selection accepts only
`proven`, as defined in [formal verification](formal-verification.md).

## Additional source and synthesis profiles

Common vendor imports now own the architecture declarations. Old flat imports
forward to them; short names remain available, while fully qualified identities
change to the canonical namespaces. See the
[profile contracts](fpga-architecture-profiles.md#additional-scalar-multiplier-profiles).

| Target / scalar primitive | RTL/formal scope | Synthesis scope |
| --- | --- | --- |
| iCE40 UP5K / `SB_MAC16` | signed/unsigned 16×16, unregistered and native output-register paths | both DSP modes; 256×16 EBR |
| Nexus LIFCL-40 / `MULT18X18` | signed/unsigned 18×18, both paths | both DSP modes; 256×16 EBR |
| GW1NR9 / `MULT9X9` | signed/unsigned 9×9, both paths | both DSP modes |
| GW2A-18 / `MULT18X18` | existing signed 18×18 native-register path | DSP and 256×16 BSRAM |
| Arora V GW5A-25 / `MULT12X12` | signed 12×12 and unsigned 11×11, both paths | both DSP modes; 256×16 BSRAM |

The scalar checks cover exact results, fixed scales, reset and cycle alignment.
The new RAM checks use a full 256×16 independent address/state reference,
enables, initialization, reset-preserved contents and write-first collisions.
These are specialization-specific RTL proofs and real synthesis checks. The
new profiles have no routed, bitstream or hardware acceptance in this reference;
the earlier board-flow table applies only to its listed configurations.
Native MAC/ALU, packing, pre-add and cascade modes are not added by this slice.

<a id="unsupported-and-release-boundaries"></a>
## Resource, timing and hardware interpretation

Reservations, emitted primitive counts and measured utilization are distinct.
Arithmetic uses hard multipliers plus the documented fabric logic; dedicated
ALU/preadder/cascade bindings and native input-register/FIR modes are outside
the supported profiles. Qualified native multiplier register modes are checked
against mapped netlists. Memory/FIFO fixtures map one block, while array-level
RTL proofs and primitive mapping remain separate evidence.

Core-only synthesis counts exclude the explicit stimulus/checksum wrapper.
Routed resources and timing include that wrapper. Routed Fmax is a constrained
tool-model result, not a measured oscillator limit or a promise for another
design. Whole-macro reservations avoid double allocation of overlapping DSP modes.

The completed levels in the table are RTL and software implementation levels.
Hardware qualification requires a separate receipt identifying the actual board
revision, device marking, loaded bitstream and observed checks. A counter/LED
check establishes only its own clock/reset/GPIO behavior; DSP and RAM acceptance
requires self-checking data tests. These claims do not include IO timing margins,
physical CDC characterization or vendor-generated IP.
