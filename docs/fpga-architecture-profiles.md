<a id="gowin-and-ecp5-architecture-profiles"></a>
# Gowin and Lattice architecture profiles

These source-defined profiles use the existing Direct-SystemVerilog target
planner. They add no ZLang syntax or scheduling family. The default RTL path
remains generic. Choose a physical profile explicitly, or let the existing
bounded planner select a legal candidate for the selected device.

## Imports and profile families

Use the common vendor namespaces:

```zlang
import std.arch.lattice.dsp
import std.arch.lattice.memory
import std.arch.gowin.dsp
import std.arch.gowin.memory
```

These imports collect architecture declarations, not instantiated modules. Each
generation retains its own resource contract; importing a vendor catalog does
not make every architecture legal on every device. The old `std.arch.ecp5_*`
and `std.arch.gowin_*` imports remain forwarding namespaces with the same short
architecture names. Existing declarations now have canonical identities under
`std.arch.lattice.dsp`, `std.arch.lattice.memory`, `std.arch.gowin.dsp` or
`std.arch.gowin.memory`; rebuild identity-bound products after this move.

The generated [API inventory](stdlib-api.md) lists exact declarations. Target
descriptions are listed in the [target guide](target-platform-architecture-description.md).

The following established families apply to GW1NR9 and Lattice ECP5.
Replace `Family` below with `Gowin` or `Ecp5`.

| Architecture | Exact admitted operation | Pipeline |
| --- | --- | --- |
| `FamilyMultiply` | integer or fixed product, operands up to 18 bits, product up to 36 bits | combinational multiplier plus one fabric output stage |
| `FamilyMultiplyMREG` | same product | native multiplier register plus one fabric output stage |
| `FamilyMultiplyMREGOREG` | same product | native multiplier/output registers plus one fabric output stage |
| `FamilyMultiplyAddFabric` | product ± addend, addend − product, or exact preadd × operand; complete result at most 54 bits | same three configurations, using suffixes `MREG` and `MREGOREG` |
| `FamilySignedProductFabric` / `FamilySignedProductFabric4` | ordered two/four signed products with add/subtract and one optional final quantization | same three configurations |
| `FamilyParallelProducts1` … `FamilyParallelProducts8` | one hard multiplier per unique product in the existing bounded joint-value domain | combinational products and one shared output stage |
| `FamilySymmetricFirFabric` | existing eight-tap symmetric fixed FIR, four coefficient values, final nearest-even/saturate `fixed<16,14>` | combinational multipliers, explicit fabric pre-add/reduction and output register |
| `FamilySymmetricFirFabricDelay2` / `Delay3` | same FIR | two/three explicit fabric output stages; native DSP registers disabled |
| `FamilyBRAM` | preserved-content synchronous memory, one block, one-edge read | inference with explicit collision logic where required |
| `FamilyAsyncFifoBRAM` | compiler-owned asynchronous FIFO storage, existing consumed-pointer/Gray/prefetch controller | one destination-clock registered read |

Arithmetic latency is respectively 1, 2 or 3 edges for the listed configurations;
II is 1. Existing exact-latency compensation can add visible fabric registers.
The profiles require rising-edge clocks and synchronous active-high core reset.
Reset clears pipeline data. Clock enables are tied high; these profiles do not
provide a ready/valid stall controller. A bubble is still one clocked input
value. Gate acceptance or interpret validity in the caller.

Unsigned modes use the primitive's static unsigned control, retaining all 18
operand bits. Signed operands retain sign extension. Fixed scales and final
conversion come from typed IR. An addend must already match the product
scale; other scale combinations require explicit alignment or generic fallback. Pre-addition retains its exact extra bit: two
signed 18-bit operands need 19 bits and cannot use this one-multiplier profile.
No intermediate narrowing is introduced to fit a primitive. The FIR uses a
typed balanced pairwise fabric reduction and one final exact quotient/guard/
sticky nearest-even conversion, followed by saturation. Its connected stateless
conversion cone is universally checked against the independent typed reference
inside the unchanged proof deadline before temporal induction.

Parallel products retain the existing 2–4 signed joint components, at most six
signed inputs of width 1…16 and at most eight unique products. This integration
does not enlarge the bilinear certificate/search domain.

## Resource ownership and realization

Each arithmetic node conservatively reserves one whole Gowin DSP macro or Lattice ECP5
DSP slice. The 20/28/156 descriptive multiplier inventories are not a second
allocatable pool. The reservations are 10 macros for GW1NR9 and 14/78 slices
for Lattice ECP5-25F/85F. Sub-block allocation is not performed by the planner.

The admitted bindings emit `MULT18X18` on Gowin and the exact pinned OSS
`MULT18X18D` ABI on Lattice ECP5. Pre-adders, reductions, addend alignment and remaining
output delays are explicit fabric. They are **DSP + fabric**, not dedicated
ALU/cascade profiles. No dedicated link is published. Lattice documentation's
`MULT18X18C`/`ALU54A` names are not assumed to alias OSS `MULT18X18D`/`ALU54B`.
Native input-register modes, hard ALU/preadder/cascade bindings and native DSP
registers inside the FIR reduction are unsupported. Select the listed MREG/OREG
profiles for multiply/MAC/reduction, and fabric-delay profiles for FIR.

Documented DSP limits follow [Gowin UG287](https://cdn.gowinsemi.com.cn/UG287E.pdf)
and the [Lattice ECP5 sysDSP guide](https://www.latticesemi.com/view_document?document_id=50469).

Manifests distinguish whole-macro reservations and emitted hard multipliers.
Memory manifests report an inferred array; only real synthesis reports identify
the mapped BRAM primitive and its physical count.
Synthesis reports contain actual mapped cell counts. Routed reports describe
the constrained physical fixture, not an estimate or isolated-core Fmax. DSP
modes can overlap physical sites: never add mode counts as independent capacity.
Clock buffers, PLL/DLL, carry and distributed RAM descriptions do not add
automatic clock/PLL/DDR generation. Shift-register capability denotes a fabric
implementation, not an invented dedicated SRL primitive.

## Memory and FIFO contract

One block admits the following logical width/depth ceilings; both dimensions
are checked together, rather than only testing their product.

| Width | Maximum depth |
| --- | --- |
| 1 / 2 / 4 | 16384 / 8192 / 4096 |
| 8 / 9 | 2048 |
| 16 / 18 | 1024 |
| 32 / 36 | 512 |

True-dual memory admits widths 1, 2, 4, 8, 9, 16 and 18 only. Contents must be
`preserve`; the registered read result must reset to `clear`. Initialization
uses the existing semantic memory initialization. Bulk-cleared arrays and
selected byte masks are not claimed as BRAM by these bindings.

Simple-dual `write_first` uses an explicit bypass when needed. `no_change`
holds the previous read result on an enabled same-address write. `read_first`
is admitted only for a shared read/write address (or a constant-disabled writer)
and widths 8, 9, 16, 18, 32 or 36. General independent-address read-first and
true-dual read-first remain rejected. Same-clock true-dual write-first/no-change
preserve declared write priority and read enables through explicit logic.

FIFO mapping is limited to the compiler-owned `crossing async_fifo(DEPTH)`
controller. It does not enable arbitrary `async_mem` bindings or introduce a
second FIFO controller. The full guard tracks consumed data, including a live
prefetched output beat. Registered read and stall behavior are retained.
Independent digital clock/paused-clock proofs use coordinated reset epochs:
both domains must receive reset before traffic resumes. They do not prove
metastability, physical reset release or unilateral-domain reset safety.

## Additional scalar multiplier profiles

These profiles extend scalar multiplication only. MAC/ALU, packed operations,
native pre-adders and cascade links are not implied by their resource catalogs.

| Device target | Architecture / native cell | Operand limits | Whole DSP inventory |
| --- | --- | --- | --- |
| `ice40up5k_sg48i` | `LatticeIce40Multiply` / `SB_MAC16` | signed or unsigned, up to 16 bits each | 8 macros |
| `lifcl_40_9bg400c` | `LatticeNexusMultiply` / `MULT18X18` | signed or unsigned, up to 18 bits each | 14 blocks |
| `gw1nr_lv9qn88pc6` and `gw1nr_lv9qn88pc7` | `GowinMultiply9` / `MULT9X9` | signed or unsigned, up to 9 bits each | shared 10-macro pool |
| `gw2a_lv18pg256c8` | `GowinMultiply` / `MULT18X18`, or `GowinMultiply9` / `MULT9X9` | up to 18 or 9 bits respectively | shared 24-macro pool |
| `gw5a_lv25mg121nc1` | `GowinAroraVMultiply` / `MULT12X12` | signed up to 12 bits; unsigned up to 11 bits | 28 macros |

Append `OREG` to the new scalar names for a native output register. The
unregistered profile has one fabric output stage (latency 1); `OREG` adds the
native register (latency 2). II is 1, CE is constant high, and reset is
synchronous active-high. On iCE40 the native output register resets through a
synchronous zero-load. Existing `GowinMultiply` retains its `MREG` and
`MREGOREG` variants rather than gaining an `OREG` alias.

The Arora V cell has signed operands. An unsigned operand requires a leading
zero within its 12-bit input: `u11` fits, `u12` does not. No product bits are
discarded to make a profile fit. Fixed-point products retain typed scales and
the existing conversion rules. A required unsupported profile is an error;
preferred selection can fall back to the generic implementation.

Each node reserves a whole macro/block, including when it uses only one
sub-multiplier. Gowin 9-bit and 18-bit modes share the same resource pool;
their capacities must not be added. Nexus `MULT18X18` and Gowin `MULT18X18`
have different primitive interfaces and are not interchangeable.

For example, compile a two-edge signed iCE40 product:

```zlang
module Product {
    clock clk reset rst
    in a,b : s16
    out result : s32
    result = pipeline(2) { a * b }
}
```

```sh
zlang product.zhl --target ice40up5k_sg48i \
  --target-architecture LatticeIce40MultiplyOREG \
  --target-architecture-mode required --systemverilog build/product.sv
```

The new device records provide bounded DSP/RAM inventories, not complete
fabric floorplans, IO packages or board constraints. Their RTL and synthesis
support does not add a `zlang fpga --board` route for those devices.

## Additional synchronous RAM profiles

| Device target | Architecture | Synthesis cell |
| --- | --- | --- |
| `ice40up5k_sg48i` | `LatticeIce40BRAM` | `SB_RAM40_4K` |
| `lifcl_40_9bg400c` | `LatticeNexusBRAM` | `PDP16K` |
| `gw2a_lv18pg256c8` | `GowinGW2ABRAM` | `SDPB` |
| `gw5a_lv25mg121nc1` | `GowinAroraVBRAM` | `SDPB` |

Each new RAM profile admits exactly 256 words of 16 bits: one write port and
one registered read port on the same rising-edge clock, `write_first`
collisions, initialized contents, preserved contents on reset and cleared
read-data output. Read and write enables retain their source behavior. These
profiles reject other geometries, byte masks, true-dual ports and asynchronous
FIFO storage. The broader GW1NR9/Lattice ECP5 contracts above are unchanged;
they do not transfer to GW2A merely because the vendor or cell name matches.

RAM uses the existing inference emitter; the listed cell is a synthesis result,
not a claim that emitted generic RTL directly instantiates that cell. Source
inventories, emitted models and real mapped counts are recorded separately.

## Runnable route and evidence

The portable kernels are in
[`arithmetic.zhl`](../examples/fpga/architectures/arithmetic.zhl) and
[`storage.zhl`](../examples/fpga/architectures/storage.zhl). For example:

```sh
zlang fpga examples/fpga/architectures/arithmetic.zhl \
  --top Multiply2 --board orangecrab-r0.2-25f \
  --architecture Ecp5MultiplyMREG --architecture-mode required \
  --self-test-wrapper --toolchain-manifest /path/to/toolchain.json \
  --output build/ecp5-multiply
```

`--self-test-wrapper` explicitly adds changing registered stimulus, clock/reset
wiring and an output checksum around the compiler-published ABI. Debug and
programming logic are not added. Core-only synthesis resources and full-fixture
resources are separate records; routed timing includes the fixture. Without
this option the user supplies the complete board-level design and pin mapping.

`tools/qualify_fpga_architectures.py` runs each admitted configuration on GW1NR9,
Lattice ECP5-25F and Lattice ECP5-85F with fixed stage deadlines and exact source/tool/library/
chip-database identities. Mandatory failures retain logs and block acceptance.
The [qualification reference](fpga-target-integration-report.md) lists exact
proof domains, completed software build configurations and hardware acceptance
requirements. Core synthesis resources, routed fixture timing and board results
are separate evidence levels.
