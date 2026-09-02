# ZLang HDL standard library index

`std` is the compiler-shipped logical namespace. It maps to tracked ordinary
ZLang HDL sources under `stdlib/`; it is intentionally shorter than the physical
directory name and is not a Python or user-package import.

| Import | Purpose |
| --- | --- |
| `std.math.fixed` | Pure exact-width/inferred fixed-point abs, clamp, min/max, MAC and explicit quantization helpers, plus compatibility components |
| `std.math.complex` | Generic `Complex<T>`/`Butterfly<S,D>`, nominal operators, explicit complex quantization |
| `std.math.complex_fixed_18_16` | Compatibility Q2.16 complex multiply profile |
| `std.stream.core` | Generic `FrameBeat<T,M>`, ready/valid register slice, skid buffer, and FIFO wrapper |
| `std.stream.serialization` | Bounded power-of-two vector serializer and raw bit collector |
| `std.stream.complex_fixed` | Compatibility Q5.32-to-Q2.16 complex stream quantizer |
| `std.dsp.fft` | Exact FFT butterfly, bit reversal, and compile-time twiddle helpers |
| `std.storage` / `std.storage.core` | Bounded delay/FIFO plus raw-bit reorder and ordered ping-pong storage wrappers |
| `std.coding` / `std.coding.core` | Parity, bit reversal, checked polynomial-tap LFSR step, and exact convolution helpers |
| `std.bus.reg` | RegBus and the source-authoritative CSR target |
| `std.bus.axi_lite` | AXI4-Lite protocol and RegBus frontend |
| `std.bus.apb` | APB protocol and RegBus frontend |
| `std.bus.axi_stream` | AXI4-Stream beat/profile and backpressure-preserving pipe |
| `std.bus.wishbone` | Wishbone B4 Classic and RegBus frontend |
| `std.target.generic` | Resource-free generic target identity |
| `std.target.asic.generic` | Generic ASIC cell/resource capability profile |
| `std.target.intel.cyclone_v` | Bounded Cyclone-V target/resource inventory |
| `std.target.xilinx.series7` | Bounded source-described Series-7 DSP48E1 profile |
| `std.target.xilinx.xc7z030` | XC7Z030 part and bounded resource inventory |
| `std.arch.xilinx7_fir` | Manual four-resource symmetric-FIR cascade template |
| `std.arch.xilinx7_memory` | Bounded Series-7 storage/resource mapping descriptions |
| `std.arch.xilinx7_signed_product` | Signed product-reduction architecture descriptions |
| `std.target.toy_asic`, `std.arch.toy` | Vendor-neutral target/architecture IR fixtures |

Imports are resolved transitively, cycles are rejected, and logical path plus
SHA-256 content hash participate in canonical/backend artifact identity.
External path/Git packages use the separate pinned `zlang.toml`/`zlang.lock`
project resolver. Filesystem-relative source imports and implicit network lookup
are not part of the compiler-shipped `std` resolver, and ordinary compilation
never fetches dependencies.

`std.math.complex` is ordinary source-authoritative ZLang. It has no bus or
stream dependency and uses only generic structs/functions and nominal operator
declarations; semantic analysis and both backends have no Complex special case.
Mixed fixed-point multiplication retains its full exact width and scale, and
the caller places every quantization boundary explicitly. In particular, the
core contains no implicit `fixed<18,16>` butterfly. The historical Q2.16
component and stream profiles live in separate compatibility imports
`std.math.complex_fixed_18_16` and `std.stream.complex_fixed`; importing
numerical Complex arithmetic does not select either profile or import a bus.

## Fixed-point math

`std.math.fixed` provides pure generic `fixed_abs`, `fixed_min`, `fixed_max`,
`fixed_clamp`, and exact `fixed_mac` functions plus explicit rounding/overflow
helpers such as `fixed_quantize_nearest_even_saturate<T>`. `fixed_abs` widens
according to ordinary unary-negation rules, so the most-negative input has an
exact positive result; `fixed_mac` preserves the full product and addition
widths. The library also retains the source-authored
`FixedAbs`, `FixedClamp`,
`FixedMinMax`, `FixedMAC`, `FixedSaturatingAdd`, and
`UFixedSaturatingAdd` components. They operate on canonical fixed-point
types; concise `SF8.8`, `SF_Sat8.8`, `UF8.8`, and `UF_Sat8.8` forms are
language aliases rather than library implementations. Encoding and conversion
rules are specified in [fixed-point-types.md](fixed-point-types.md).

## Complex values, streams, storage, and coding

`std.math.complex` provides the ordinary parameterized value type `Complex<T>`
with `re` and `im` fields. `butterfly_quantized<T>` requires the output numeric
profile explicitly; there is no profile-selecting short spelling in the core.
Complex remains ordinary source, not a compiler or backend primitive.

`std.bus.axi_stream` keeps `AXIStream<DW>` as the byte-lane bus profile and also
provides `AXIStreamOf<T>` for a semantic payload type:

```zlang
input  : AXIStreamOf<Complex<fixed<18,16>>>.sink @clk
output : AXIStreamOf<Complex<fixed<18,16>>>.source @clk
```

`AXIStream<32>` has `data/keep/strb/last`; `AXIStreamOf<T>` transports exactly
one typed `T` per transfer. A future physical wrapper may serialize `T` to a
chosen TDATA layout without changing its semantic payload identity.

Generic stream composition uses `std.stream.core`, independently of AXI:

```zlang
import std.stream.core

struct Meta { tag : u2 }
type Beat = FrameBeat<u8,Meta>

module Queue {
    clock clk
    reset rst
    in input : rv<Beat>
    out output : rv<Beat>

    inst storage : RvFifo<T=Beat,D=4>
    connect input -> storage.input
    connect storage.output -> output
}
```

`RvRegisterSlice<T>`, `RvSkidBuffer<T>`, and `RvFifo<T,D>` reuse the exact
language FIFO semantics, including simultaneous push/pop and payload stability
under stall. `std.stream.serialization` currently supports a power-of-two
`RvVectorSerializer<T,N,IW>` and a raw `RvBitCollector<N,IW>`, where
`IW = floor_log2(N)`. Their declarations enforce `N >= 2`, power-of-two `N`,
and exact `IW` through compile-time `where` constraints.

`std.dsp.fft` keeps arithmetic intent explicit. Its butterfly does not
quantize, twiddle generation uses compile-time `sin`/`cos` and quantizes only
to its caller-selected type, and bit reversal is a deterministic sequence-order
operation. Value parameters inferred through a
`bits<N>` argument are not implemented yet, so bit-width helpers use explicit
calls such as `fft_bit_reverse<N=8>(value)`.

`std.storage.core` supplies `StorageDelay1<T>`, `StorageDelay2<T>`, the
generic `StorageQueue<T,D,CW>` (`CW = ceil_log2(D + 1)`), and bounded raw-bit
`StorageReorderBits<W,N>`/`StoragePingPongBits<W,N>` banks. The latter use
power-of-two depth, exact runtime indices, and explicit bitcast at typed/raw
boundaries. Ping-pong publication is ordered: a second `commit` is blocked
until the visible read bank retires, while simultaneous retire/commit is legal.
It also provides immutable, source-authored generic ROM wrappers:

```zlang
inst direct : StorageRom<T=u8,N=8,IW=3,image=image>
inst generated : StorageGeneratedRom<
    T=u8,N=8,IW=3,producer=fn make_image<T=u8,N=8>
>
```

Both wrappers elaborate to the existing typed `Rom` IR with concrete immutable
contents and the same companion image for Clash and direct SystemVerilog.
`StorageRom` accepts a fully evaluated exact `vec<N,T>` constant;
`StorageGeneratedRom` invokes a statically selected pure zero-argument producer
during elaboration. Constants and producers are specialization parameters, not
runtime ports or backend callbacks. Fixed, struct, vector, and nested-vector
word types retain their exact canonical layout.

The core language currently requires a literal in
`delay<N>`, so a generic-depth delay wrapper would be dishonest; longer delays
remain explicit source until parameterized delay depth is supported.
`std.coding.core` supplies representation-level parity, bit reversal and
one-step LFSR operations plus exact dot/convolution and `table_gather`
helpers. `table_gather<T,N,IW>` is accepted only when the complete proven
`uint<IW>` range fits the source vector. It preserves order but deliberately
does not claim bijection: duplicate and omitted elements remain legal. The
`CodingLfsrStep<N>` module enforces `N >= 2`; the compatibility pure function
cannot carry a `where` clause because function constraints are not yet syntax.

The current language deliberately prevents several tempting but invalid
"generic" wrappers. A target-independent `reg vec<N,T>` cannot be initialized
without either an explicit caller-provided value or a future `default<T>`
contract, so the shipped reusable reorder/ping-pong banks use an explicit
`bits<W>` representation boundary. A generic runtime gather now
retains the conservative element-type range through a table-loaded index, but
it does not prove that a table is a mathematical permutation. The negative
semantic tests retain the bounded gather witness; no backend guesses an
initializer or claims a permutation proof. Concrete projects may still use typed ROMs and
permutations with known source-generated tables, as the Wi-Fi and FFT sources
do.

The separated `std.stream.complex_fixed` compatibility kernel exposes raw
`rv<Complex<...>>` ports. The former aggregate AXIStream profile cannot be
forwarded transparently yet: hierarchical connections between
`AXIStreamOf<Complex<...>>` specializations and member-level child ready/valid
ports are rejected by the current aggregate binding model. The kernel is fully
backend-validated, but restoring that aggregate ABI requires a generic
aggregate-member binding compiler slice rather than bus-specific stdlib code.

Every shipped `.zhl` file is discovered recursively by the `std.*` resolver.
Clean-wheel tests compare the complete recursive source tree with wheel contents;
adding a library file without packaging it therefore fails the build rather than
creating a checkout-only import.

## Target and architecture descriptions

Target libraries use the same safe, hashed `std.*` resolver as bus and math
sources. They describe resource capabilities and physical binding locators; they
do not add functional primitives. See
[target-platform-architecture-description.md](target-platform-architecture-description.md)
for the supported declarations, manual selection flow, and current bounded
Series-7 implementation.
