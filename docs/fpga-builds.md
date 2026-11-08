<a id="gowin-and-ecp5-fpga-builds"></a>
# Gowin and Lattice FPGA builds

ZLang emits ordinary Direct SystemVerilog. `zlang fpga` runs a bounded external
synthesis, place-and-route and bitstream flow; it does not add another backend,
change simulation semantics, program a board, or create another mapping planner.

## Devices and board revisions

| Board profile | Device | Clock | Clock / active-low reset / active-low LED pads |
| --- | --- | --- | --- |
| `brs-100-gw1nr9-r1.0` | GW1NR-LV9QN88PC7/I6, QFN88P | 27 MHz | 52 / 3 / 16 |
| `epm11-r1.0` | GW1NR-LV9QN88PC7/I6, QFN88P | 27 MHz | 52 / 3 / 10 |
| `orangecrab-r0.2-25f` | LFE5U-25F-8MG285C, csfBGA285 | 48 MHz | A9 / J17 / K4 |
| `orangecrab-r0.2.1-25f` | LFE5U-25F-8MG285C, csfBGA285 | 48 MHz | A9 / J17 / K4 |
| `orangecrab-r0.2.1-85f` | LFE5U-85F-8MG285, csfBGA285 | 48 MHz | A9 / J17 / K4 |

These minimal pin sets use LVCMOS33 and a pulled-up reset button. The input
`rst_n` contract is synchronous active-low reset. Mechanical button debounce
and external reset-release conditioning are not inserted. The counter examples
are demonstrations, not a board reset-safety qualification.

The manufacturer's BRS/EPM11 projects select C7/I6. The C6/I5 device
has a separate source target `gw1nr_lv9qn88pc6` (part prefix
`GW1NR-LV9QN88PC6`); this does not assert that a C7/I6 board contains C6/I5
silicon. `gw1nr9-qn88p-c6i5-reference` is a separate C6/I5 device pin fixture:
it reuses the explicitly recorded BRS r1.0 clock/button/LED connections for
software flow validation. Select it for the C6/I5 device flow, not as an alias
for either populated board. See the [qualification reference](fpga-target-integration-report.md)
for the device and board configurations covered by each evidence level.
The slash in the full Gowin order code is stored in board metadata,
not introduced into the language's existing device-part grammar.

OrangeCrab revisions are explicit. r0.2 uses a 2x5 JTAG header, whereas r0.2.1
uses a 1x5 header. Neither profile is an alias for r0.1. The pinned r0.2.1
85F/4G BOM omits the temperature-grade suffix; do not infer C/I or substitute
a Lattice ECP5-5G part. Check the actual device marking before programming.

Board facts and pinned source references live in
[`fpga-boards.json`](../zlang/data/fpga-boards.json). They are derived separately
from the [BRS schematic](https://brisbanesilicon.com.au/docs/BRS-100-GW1NR9_Schematic.pdf),
[EPM11 schematic](https://brisbanesilicon.com.au/docs/EPM11_Schematic.pdf), and
[OrangeCrab hardware sources](https://github.com/orangecrab-fpga/orangecrab-hardware/tree/c511d569fe2af39467041f888bc231020f40c6ac/hardware).

## Reproducible tools and invocation

Install the compiler using `make install`. FPGA tools are external dependencies,
not Python runtime dependencies. Use the pinned Linux-x64 OSS CAD Suite
2026-10-21 archive from the
[upstream release](https://github.com/YosysHQ/oss-cad-suite-build/releases/tag/2026-10-21).
The setup command checks its SHA-256 and the complete installed executable,
library, Python and device-database inventory. Choose fresh paths in your
shared EDA directory; existing installations/manifests are never overwritten.

```sh
.venv/bin/python -m tools.setup_fpga_toolchain \
  --archive "$EDA_ROOT/downloads/oss-cad-suite-linux-x64-20261021.tgz" \
  --destination "$EDA_ROOT/install/fpga-2026-10-21" \
  --manifest "$EDA_ROOT/manifests/fpga-2026-10-21.json"
.venv/bin/zlang fpga examples/fpga/counter.zhl \
  --board orangecrab-r0.2-25f \
  --toolchain-manifest "$EDA_ROOT/manifests/fpga-2026-10-21.json" \
  --output build/orangecrab-counter
```

`EDA_ROOT` is an explicitly chosen tools directory, not an automatic search
path. The manifest
must point to the exact pinned installation. There is no ambient-tool fallback.
For an already extracted exact archive, omit `--archive` to bind it to a fresh
manifest. Tool wrappers run with isolated Python/library settings.

Output directories must be fresh. Every compiler-owned physical ABI bit must
have a pin assignment; the default examples use `clk`, `rst_n`, `led`.
`--pin MEMBER=BOARD_NET` can rename those connections. Supplying any assignments
requires a complete map. Unknown pins, direction mismatches, duplicate pads,
wrong reset contracts and incomplete maps fail closed. Additional unreviewed
header pins are deliberately unavailable in v1.

Gowin OSS uses `synth_gowin -family gw1n`, nextpnr-himbaechel with the exact
order code and `family=GW1N-9C`, then Apicula `gowin_pack`.
Lattice ECP5 uses `synth_ecp5`, nextpnr-ecp5 with exact density/package/speed, then
`ecppack`. One source clock is constrained to the board oscillator; there is
no automatic PLL, DDR controller, timing-ignore or unconstrained-pin mode.

## Evidence and limits

The build retains emitted RTL/backend manifest, source/dependency identities,
SDC and CST/LPF, tool/dependency inventories, commands, logs, netlists, routed
reports and a hash-bound `receipt.json`. Each external stage has a fixed
120-second deadline. Failure or timeout keeps partial logs and a failed receipt;
a nonzero command, missing report, unmet frequency or stale tool inventory
cannot become a successful bitstream result.

Synthesis cells and routed primitive utilization are separate measurements.
Different Gowin DSP modes overlap physical resources. Lattice ECP5 multipliers are
18x18 units, not sysDSP-slice counts. nextpnr's `DUMMY_CELL` is placement
bookkeeping with no physical-capacity denominator. Raw reports retain all these
entries. Routed Fmax is a tool-model estimate after routing, not measured board
frequency or silicon characterization.

The five [portable examples](../examples/fpga/README.md) cover a counter, GPIO,
exact fixed-point multiplication, synchronous memory and hierarchy. Run their
real matrix with:

```sh
.venv/bin/python -m tools.qualify_fpga \
  --toolchain-manifest "$EDA_ROOT/manifests/fpga-2026-10-21.json" \
  --output build/fpga-qualification
```

Compile-only, synthesized, routed, packed and hardware-tested support are
separate states. The [qualification reference](fpga-target-integration-report.md)
lists the completed level for each board profile and the separate hardware
acceptance requirements.

## Programming and unsupported capabilities

Programming is a separate, explicit operation. BRS and EPM11 support
openFPGALoader board selectors `brs-100-gw1nr9` and `epm11`; their USB JTAG
firmware/cable must match the board. Volatile SRAM loading uses `-m`.
OrangeCrab requires a compatible external JTAG cable for SRAM loading. Its USB
DFU workflow depends on an installed board-specific bootloader and image layout;
this build flow does not generate a DFU container or overwrite flash/bootloaders.

For a relocatable Gowin EDA batch project using the same production RTL,
compiler ABI and board constraints:

```sh
.venv/bin/zlang fpga examples/fpga/counter.zhl \
  --board brs-100-gw1nr9-r1.0 --flow gowin-eda-project \
  --output build/gowin-vendor-project
```

The export contains `build.tcl`, `dut.sv`, CST, SDC and a source-bound receipt.
The export uses the manufacturer's SUG1220-2.1E batch commands.
Run `gw_sh build.tcl` from that directory with a separately installed and
licensed Gowin EDA. The `gowin-eda-project` flow prepares this project; it does
not invoke the vendor tools. Bind vendor resource/timing reports to the exact
project and tool identities when validating that execution.

The executable OSS path includes the bounded DSP + fabric and inferred BRAM/FIFO profiles
in the next section; their actual primitive modes and utilization are recorded
per witness. Dedicated hard ALU/preadder/cascade modes, automatic PLL generation,
SerDes, DDR, multi-clock physical constraints, arbitrary board revisions and
unbounded target search remain outside this integration.

## Explicit architecture selection

`--architecture`, `--architecture-mode` and `--target-evidence-policy` pass the
existing target-planning options to normal compilation. `measured_required`
still rejects missing or mismatched evidence; fixture timing is not silently
imported as core QoR. The [stdlib architecture profiles](fpga-architecture-profiles.md)
list legal DSP/memory shapes and runnable kernels. `--self-test-wrapper` adds an
explicit ABI-derived stimulus/checksum fixture for physical qualification; its
resources and timing are identified separately from isolated core synthesis.
These options currently use the OSS execution flow. Gowin EDA project export
retains the generic production RTL route; explicit architecture execution uses
the OSS flow.
