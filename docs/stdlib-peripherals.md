# Basic GPIO, timer and serial cores

These components are ordinary ZLang sources under `std.peripheral.*`. They
provide small, fixed-width interfaces. The [API inventory](stdlib-api.md)
contains exact declarations and parameter constraints.

| Import | Component | Public contract |
|---|---|---|
| `std.peripheral.gpio` | `Gpio8` | Eight combinational input/output/OE bits; no registers or IRQ. |
| `std.peripheral.timer` | `PeriodicTimer32<PERIOD=1000>` | Fixed 32-bit counter, constant positive period, enable/pause and tick. |
| `std.peripheral.pwm` | `Pwm16<PERIOD=256,DUTY=128>` | One 16-bit edge-aligned channel with constant period and duty. |
| `std.peripheral.capture` | `RisingCapture32` | Rising edges, caller-supplied 32-bit timestamp, one result slot. |
| `std.peripheral.uart` | `UartTx8N1<DIVISOR=16>`, `UartRx8N1<DIVISOR=16>` | Eight data bits, no parity, one stop bit, LSB first, constant clock divisor. |
| `std.peripheral.spi` | `SpiMasterMode0<HALF_PERIOD=2>` | Eight-bit full-duplex master, MSB first, mode 0, one chip select. |
| `std.peripheral.i2c` | `I2cWriteByte<HALF_PERIOD=4>` | Seven-bit address, one write byte, automatic START/STOP. |

Every core also has a small `RegBus<32,32>.responder` wrapper described below.
There are no IRQ controllers, software drivers, DMA, vendor-compatible register
profiles or runtime baud/mode configuration. The wrappers instantiate only the
simple cores above; they do not hide a more capable controller.

## Clock, reset and pins

Stateful cores use one rising-edge `clk` and synchronous active-high `rst`.
Connect the separate ordinary input `reset_active` to the **same physical
reset**. It suppresses acceptance, result validity and active pin controls
during reset. Reset discards an active transfer and pending results. Test
infrastructure and input vectors are not part of these source APIs.

Pin inputs named `*_sampled` and capture `level` must already belong to the
sampling clock domain. Pad buffers, synchronization, setup/hold, pull-ups and
board timing are caller-owned. No core adds CDC. `Gpio8` is combinational;
its `reset_active` gates output enables, and its `sampled` output equals
`pins_in` without sampling or synchronization.

Ready/valid payloads retain their declared binary types. Acceptance occurs on
`valid && ready`; results remain stable while valid and stalled. Arithmetic
and U/X diagnostics follow the [language contract](types-and-numerics.md).
No electrical, metastability, routed timing or board-capture qualification is
implied by source availability or simulation. A proof of one specialization
does not establish all parameter choices.

## GPIO, timer and PWM

`Gpio8` drives `pins_out=data` and `pins_oe=reset_active ? 0 : oe`. The caller
selects eight output-enable bits. There is no edge detector or interrupt.

`PeriodicTimer32` permits `1 <= PERIOD <= 0xffffffff`. `count` starts at zero.
An enabled edge increments it, wrapping at `PERIOD-1`. `tick` is high during
the enabled terminal cycle, so PERIOD=1 ticks every enabled edge. Disable
pauses the count. There is no runtime period write, restart command or separate
free-running timestamp.

`Pwm16` permits `1 <= PERIOD <= 65535` and `0 <= DUTY <= PERIOD`. `pin` is high
while enabled and `phase < DUTY`. Disabled edges return phase to zero; reset
does the same. Duty 0 is continuously low; duty equal to period is continuously
high while enabled. There is no runtime configuration queue, polarity option
or shadow register bank.

## Rising-edge capture

`RisingCapture32` takes `enable`, `level` and `timestamp:u32`, and produces
`output:rv<u32>` plus `overflow`. The first active sample after reset establishes
history. Subsequent enabled low-to-high transitions capture the pre-edge
timestamp. A full slot drops the new edge and sets sticky overflow; only reset
clears overflow. A retiring result may be replaced on that same edge. Stalls
do not change a pending timestamp. Falling edges and sequence counters are
not provided.

## UART 8N1

DIVISOR is a specialization constant in 4…65535, counting sampling-clock
edges per bit. `UartTx8N1` takes `input:rv<bits<8>>` and emits `tx` and `busy`.
Idle/reset TX is high. A byte is one low start bit, eight LSB-first data bits
and one high stop bit. A new byte may be accepted on the last stop-bit edge.

`UartRx8N1` takes `rx_sampled` and produces `output:rv<UartRxBeat>`, `busy` and
`overrun`. `UartRxBeat` has `data:bits<8>` and `framing_error:bit`. The receiver
qualifies the start at floor(DIVISOR/2), then samples every DIVISOR clocks.
A low stop reports framing error and waits for high before another frame, so
a held break produces one error beat. One result slot is provided. An incoming
byte cannot backpressure the pin: a full slot drops that byte and retains
overrun until reset. No FIFO, flow control or alternate framing is included.

## SPI mode 0

HALF_PERIOD is a constant in 1…65535. `SpiMasterMode0` takes
`input:rv<bits<8>>`, returns `output:rv<bits<8>>`, and exposes `sck`, `mosi`,
`cs_n`, `busy` and `miso_sampled`. CPOL=0 and CPHA=0. Each received bit is sampled
on a leading edge; MOSI advances on trailing edges. A setup edge precedes
chip-select assertion. Result availability is `2+16*HALF_PERIOD` edges after
acceptance without reset. A result slot can retire and accept the next command
on the same edge. Each command deasserts CS at completion; continuous-CS
transactions and modes 1–3 are not available.

## I²C single-byte write

HALF_PERIOD is a constant in 1…65535. Input payload is
`I2cWriteRequest { address:u7, data:bits<8> }`. One accepted request generates
START, `(address << 1) | 0`, address ACK, one data byte, data ACK and STOP.
The output is one `rv<u3>` status:

| Status | Meaning |
|---|---|
| 0 | Both bytes acknowledged and STOP completed. |
| 1 | Address NACK; STOP completed. |
| 2 | Data NACK; STOP completed. |
| 3 | Clock-stretch timeout; lines released. |
| 4 | Bus occupied at acceptance or released-data arbitration fault; lines released. |

`scl_low` and `sda_low` are **low-drive enables**. A physical pad drives 0 when
enabled and otherwise releases to an external pull-up. Never connect them as
high-drive data values. Counting an observed high interval pauses while SCL
is stretched. The fixed timeout is 1024 consecutive low samples during a
requested high interval. These are sampled digital contracts, not I²C speed
grade or multi-master qualification.

One command and one stable result are supported. The next command waits for
result retirement; same-edge retirement/acceptance is allowed. Reads, repeated
START, multi-byte transactions, target mode, runtime abort and recovery pulses
are not implemented. Reset is the destructive abort mechanism.

## Source example

[`examples/peripherals_basic.zhl`](../examples/peripherals_basic.zhl) connects
the cores with explicit pin and ready/valid ports. Check it with:

```sh
zlang --check examples/peripherals_basic.zhl
```

The example is synthesizable source, not a host driver or automated testbench.
Supply clocks, reset, pin models and traffic through the ordinary simulation
or physical-wrapper workflows in the [User Guide](user-guide.md).

## RegBus32 wrappers

All seven peripheral families have a responder wrapper. Registers are ordinary
ZLang `csr` declarations. The shared `std.peripheral.regbus` transport captures
one pre-edge response per accepted request. Effects occur on that accepting
edge, once; holding a response does not repeat writes, transmit, or POP. While
a response is pending, request ready is low. There is no same-edge response
retirement/new-request acceptance. Response latency is one edge. Reset discards
pending requests/results and resets RW words to zero.

| Wrapper | 32-bit aligned byte offsets |
|---|---|
| `Gpio8RegBus32` | 0 INPUT (RO); 4 OUTPUT (RW); 8 OE (RW) |
| `PeriodicTimer32RegBus32<PERIOD=1000>` | 0 COUNT (RO); 4 CONTROL (RW); 8 PERIOD (RO) |
| `Pwm16RegBus32<PERIOD=256,DUTY=128>` | 0 PHASE (RO); 4 CONTROL (RW); 8 PERIOD (RO); 12 DUTY (RO) |
| `RisingCapture32RegBus32` | 0 STATUS (RO); 4 CONTROL (RW); 8 DATA (RO); 12 POP (command) |
| `Uart8N1RegBus32<DIVISOR=16>` | 0 STATUS (RO); 4 TX (command); 8 RX (RO); 12 POP (command); 16 DIVISOR (RO) |
| `SpiMode0RegBus32<HALF_PERIOD=2>` | 0 STATUS (RO); 4 TX (command); 8 RX (RO); 12 POP (command); 16 HALF_PERIOD (RO) |
| `I2cWriteByteRegBus32<HALF_PERIOD=4>` | 0 STATUS (RO); 4 REQUEST (command); 8 RESULT (RO); 12 POP (command); 16 HALF_PERIOD (RO); 20 STRETCH_LIMIT (RO, 1024) |

CONTROL bit 0 enables timer, PWM or capture; other bits are stored and read
back without effect. OUTPUT/OE store all 32 bits, but only their low eight bits
connect to GPIO pins. Configuration constants remain immutable: write them and
the response reports error. Counts keep their core widths and timing contracts.

RW writes merge the four WSTRB byte lanes. A zero-strobe write to a writable
word succeeds without effect. Unknown/unaligned addresses and writes to RO
words return error and zero data, even with zero strobes. Commands read as
zero. Successful writes return the pre-write read value. All read responses
are snapshots; live status may change while a response is held.

UART/SPI TX uses bits 7:0 and only WSTRB lane 0 starts a transfer. A write to
other lanes has no effect. An effective TX while not ready returns error.
UART RX packs data at 7:0 and framing_error at bit 8. SPI RX packs eight bits
at 7:0. Reads only peek; write POP bit 0 with lane 0 enabled to consume the
result. Empty effective POP returns error. A zero POP bit has no effect.

I²C REQUEST packs data at 7:0 and address at 14:8. Every nonzero-strobe REQUEST
requires both low byte lanes; a partial low word returns error. Acceptance is
atomic, with no intermediate address/data registers. Busy or an unconsumed
result blocks the next command with an error response. RESULT contains the
core status code. POP behaves as above.

Status bit assignments:

| Peripheral | STATUS bits |
|---|---|
| Capture | 0 valid; 1 sticky overflow |
| UART | 0 TX ready; 1 RX valid; 2 framing error; 3 sticky overrun; 4 TX busy; 5 RX busy |
| SPI | 0 TX ready; 1 result valid; 2 busy |
| I²C | 0 command ready; 1 result valid; 2 busy; 5:3 result code |

Payload/framing/result fields are meaningful when their valid bit is set.
Overflow/overrun clear only on reset. There are no extra FIFOs or IRQs.
The capture wrapper still takes a caller-owned `timestamp:u32` input.

[`examples/peripherals_regbus.zhl`](../examples/peripherals_regbus.zhl) exposes
seven independent responders and explicit pins. It does not add an address
interconnect, a software driver or traffic generator. Check it with:

```sh
zlang --check examples/peripherals_regbus.zhl
```
