# Portable FPGA examples

All five examples use `clk`, synchronous active-low `rst_n`, and one active-low
board LED output `led`. They share the exact board pin constraints documented
in the [FPGA build guide](../../docs/fpga-builds.md).

| Source | Top | Purpose |
| --- | --- | --- |
| [counter.zhl](counter.zhl) | `FpgaCounter` | 24-bit sequential counter |
| [gpio.zhl](gpio.zhl) | `FpgaGpio` | Existing `Gpio8` core driven from a counter |
| [fixed_dsp.zhl](fixed_dsp.zhl) | `FpgaFixedDsp` | Dynamic signed `fixed<16,14>` product, exact `fixed<32,28>` result |
| [memory.zhl](memory.zhl) | `FpgaMemory` | Synchronous 256x16 read-first RAM and a separate address/data source |
| [hierarchy.zhl](hierarchy.zhl) | `FpgaHierarchy` | Child-module state with inherited clock/reset |

Select `--top` for the two multi-module sources. A GPIO internal bus is not an
extra FPGA pin. The fixed-point example increments raw representations with
explicit truncation; multiplication retains the full signed product. The RAM
uses the existing `contents preserve` contract: cells have deterministic zero
initialization and are not rewritten on reset; the registered read result clears.
Its LED is a data activity indicator, not a memory-valid handshake.

The examples generate internal activity; there is no external stimulus file,
UART console, PLL or programming side effect. Synthesized DSP/BRAM inference
must be read from the actual per-build report, not inferred from source spelling.

## Explicit architecture kernels

[architectures/arithmetic.zhl](architectures/arithmetic.zhl) contains the exact
multiply/MAC/preadd, ordered reduction, parallel-product and symmetric FIR roots
used by `tools/qualify_fpga_architectures.py`. Numeric suffixes 1/2/3 denote
pipeline edges; `Parallel1` through `Parallel8` denote unique hard products.
[architectures/storage.zhl](architectures/storage.zhl) contains the synchronous
RAM collision-policy roots and `AsyncFifoStorage`.

These kernels expose their compiler-owned core ABI. Use the explicit
`--self-test-wrapper` fixture to route them on a board profile; the fixture's
stimulus/checksum resources and routed timing are distinct from core-only
synthesis. See [architecture limits and commands](../../docs/fpga-architecture-profiles.md).
