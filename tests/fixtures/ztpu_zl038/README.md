<!-- SPDX-License-Identifier: Apache-2.0 -->

# ZL-038 layout-DMA scheduler witnesses

The two scalar cores and typed AXI wrapper are copied from the ZTPU layout-DMA
regression by postoroniy (https://github.com/postoroniy/ztpu). Their source
license is BSD-3-Clause; see `LICENSES/BSD-3-Clause.txt` at repository root.

`fsm/layout_dma_core.zhl` is the historical explicit 12-state source. Its
inactive `write_w_last = 1` predates the final ZTPU ABI correction; retain it
unchanged as a compiler-performance witness, not an ABI golden model.

`flat/layout_dma_core.zhl` is the flat switch-state source after adding the
two sticky reader/writer error registers. Both scalar witnesses previously
passed semantic checking but timed out in native plan preparation. The shared
wrapper is their typed AXI boundary.

`hierarchy/layout_dma.zhl` is the earlier reader/controller/writer helper
hierarchy, after its documented guard correction. On public a16 it passes
semantic checking but exceeds a 15-second direct-SystemVerilog bound; this
fixture protects that separate backend scaling path.
