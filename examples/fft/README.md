# FFT validation examples

These sources are deliberately split into two bounded validation designs:

- `complex_multiply_pipeline_auto.zl` contains two independently selectable
  pure scalar kernels, `FFTComplexMultiplyRealAuto` and
  `FFTComplexMultiplyImagAuto`. Each has one fixed-point quantization boundary,
  latency 1 for the generic fallback, and II=1. The target planner publishes
  the four DSP48E1 pipeline configurations; routed MREG evidence selects
  latency 2 at 100 MHz on `xc7z030ffg676-1`.
- `sdf_stage_numeric.zl` contains the reusable numerical DIF-SDF stage and a
  concrete `FFTSDFStageNumericD4` wrapper (`D=4`, sample `fixed<18,16>`,
  twiddle `fixed<16,14>`). Its ordinary generic `fft_twiddles` function
  initializes a one-cycle synchronous ROM; no external coefficient port or
  hand-written memory file is required. The reusable stage derives `CW`/`IW`
  from earlier `D` with `index_width(...)`; wrappers use concise child
  declarations and bare typed ready/valid connections, and emit through both real
  Clash and direct-SystemVerilog backends. The same file also contains
  `FFT4SDFReference`, which composes `D=2` and `D=1` specializations into the
  first complete two-stage streaming reference, and `FFT8SDFReference`, which
  composes the same reusable stage at `D=4`, `D=2`, and `D=1`. The bounded
  `FFT16SDFReference` extends that composition with a leading `D=8` stage, and
  `FFT32SDFReference` adds the corresponding leading `D=16` stage.

## Pure complex multiply

Generate generic Clash for the real and imaginary components:

```sh
.venv/bin/zlangc examples/fft/complex_multiply_pipeline_auto.zl \
  --top FFTComplexMultiplyRealAuto \
  -o build/FFTComplexMultiplyRealAuto.hs
.venv/bin/zlangc examples/fft/complex_multiply_pipeline_auto.zl \
  --top FFTComplexMultiplyImagAuto \
  -o build/FFTComplexMultiplyImagAuto.hs
```

Generate and Verilator-lint the generic Clash Verilog:

```sh
.venv/bin/zlangc examples/fft/complex_multiply_pipeline_auto.zl \
  --top FFTComplexMultiplyRealAuto \
  -o build/FFTComplexMultiplyRealAuto.hs \
  --verilog-dir build/fft-real-verilog --verilator-lint
.venv/bin/zlangc examples/fft/complex_multiply_pipeline_auto.zl \
  --top FFTComplexMultiplyImagAuto \
  -o build/FFTComplexMultiplyImagAuto.hs \
  --verilog-dir build/fft-imag-verilog --verilator-lint
```

Emit the supported direct-SystemVerilog path and target-planner report:

```sh
.venv/bin/zlangc examples/fft/complex_multiply_pipeline_auto.zl \
  --top FFTComplexMultiplyRealAuto --target xc7z030ffg676-1 \
  --systemverilog build/FFTComplexMultiplyRealAuto.sv \
  --pipeline-report build/FFTComplexMultiplyRealAuto.pipeline
.venv/bin/zlangc examples/fft/complex_multiply_pipeline_auto.zl \
  --top FFTComplexMultiplyImagAuto --target xc7z030ffg676-1 \
  --systemverilog build/FFTComplexMultiplyImagAuto.sv \
  --pipeline-report build/FFTComplexMultiplyImagAuto.pipeline
```

The routed evidence sweep is reproducible with:

```sh
.venv/bin/python tools/signed_product_pipeline_qor.py \
  --output /tmp/zlang-signed-product-qor \
  --vivado "$ZLANG_VIVADO" --jobs 2
```

Set `ZLANG_VIVADO` to the Vivado executable, or omit `--vivado` when Vivado is
already available on `PATH`.

## Numerical SDF stage

The generic module can be inspected semantically, and the concrete top name is
`FFTSDFStageNumericD4`:

```sh
.venv/bin/zlangc examples/fft/sdf_stage_numeric.zl \
  --top FFTSDFStageNumericD4 -o build/FFTSDFStageNumericD4.hs
```

The wrapper's backward input `ready` is driven by the child connection, while
the child's forward `payload`/`valid` drive the top output. The exact-
`Fraction` oracle compares simulator, direct-SV + Verilator, and Clash 1.11 +
Verilator traces under input gaps, output stalls, FIFO replacement, and reset
epochs. The CLI publishes the deterministic `.mem` companion beside the
selected output; direct SV consumes it with `$readmemb`, while Clash consumes
the byte-identical image with `romFile`.

## FFT4 two-stage reference

`FFT4SDFReference` connects a `D=2` stage directly to a `D=1` stage. Its
natural radix-2 DIF output-bin order is bit-reversed: `0, 2, 1, 3`. The SDF
pipeline is continuous rather than self-flushing: after four samples, further
accepted tokens advance the prior frame through the stages. A finite testbench
therefore supplies accepted padding tokens before draining the registered
output; idle cycles alone do not flush pending samples.

Generate the direct-SystemVerilog artifact with:

```sh
.venv/bin/zlangc examples/fft/sdf_stage_numeric.zl \
  --top FFT4SDFReference \
  --systemverilog build/FFT4SDFReference.sv
```

The artifact has two separate initialized-ROM companions, of exact depths 2
and 1. Direct SV passes Verilator lint. Clash 1.11 also generates the design;
its generated `romFile` RTL requires the repository's established
`-Wno-WIDTHTRUNC` Verilator waiver for Clash's widened ROM index. This is a
known generated-RTL warning, not a ZLang width or numerical-semantics change.
This FFT4 reference makes no target-planner, DSP-binding, or QoR claim; the
later complete FFT512 functional hierarchy is documented below.

## FFT8 three-stage reference

`FFT8SDFReference` connects `D=4`, `D=2`, and `D=1` specializations. Natural
input samples emerge in radix-2 DIF bit-reversed bin order
`0, 4, 2, 6, 1, 5, 3, 7`. With continuous valid/ready traffic, the first input
is accepted at relative cycle 0, the first output transfers at relative cycle
10, and the remaining outputs transfer at II=1 through relative cycle 17.

Like the FFT4 reference, this is a continuous stream and has no implicit frame
flush. A finite eight-sample test supplies seven later accepted sentinel tokens
to advance the complete frame; after the last sentinel, three idle observation
cycles expose the registered tail. Those sentinels are the beginning of the
following stream frame, not a separate flush operation.

The numerical contract quantizes after every architectural butterfly boundary.
For spans `8`, `4`, and `2`, respectively, each pair computes a nearest-even,
saturating `fixed<18,16>` sum and difference, then applies the span twiddle to
the difference and quantizes that complex product once more to
`fixed<18,16>`. The independent raw fixture

```text
(1000,200), (-300,500), (700,-100), (-200,-400),
(400,300), (-600,100), (250,-350), (-150,450)
```

produces, in stream order,

```text
(1100,700), (3600,-600), (1000,1500), (-100,400),
(779,157), (921,-1257), (-215,-711), (915,1411)
```

Simulation and both RTL backends preserve payload stability under downstream
stall, and reset discards a partial pre-reset transform rather than allowing it
to enter the next reset epoch. The emitted artifacts retain three distinct ROM
companions of exact depths 4, 2, and 1.

Generate the direct-SystemVerilog artifact with:

```sh
.venv/bin/zlangc examples/fft/sdf_stage_numeric.zl \
  --top FFT8SDFReference \
  --systemverilog build/FFT8SDFReference.sv
```

The target report for this stateful hierarchy remains generic with unknown
whole-module timing. This FFT8 reference does not itself claim target-aware
architecture selection, DSP/BRAM mapping, cardinal-twiddle strength reduction,
synthesis QoR, or Fmax. The complete FFT512 functional hierarchy is documented
below.

## FFT16 four-stage reference

`FFT16SDFReference` composes exact `D=8`, `D=4`, `D=2`, and `D=1`
specializations. Natural inputs produce the DIF bit-reversed bin stream
`0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15`. Under continuous
valid/ready traffic, an `x0` accepted at relative cycle 0 produces the first
output at relative cycle 19, followed by one result per cycle through cycle 34.

A finite sixteen-token fixture needs fifteen subsequently accepted sentinel
tokens to advance all of its results. Four idle observation cycles after the
last sentinel expose the registered tail. As in the smaller references, those
tokens are ordinary samples in the following continuous-stream frame; ZLang
does not assign them implicit flush semantics.

The exact oracle applies a nearest-even, saturating `fixed<18,16>` quantization
after every sum and difference, then another such quantization after the exact
complex multiply by the `fixed<16,14>` stage twiddle. It applies this contract
at spans `16`, `8`, `4`, and `2`. The deterministic raw fixture

```text
(1000,200), (-300,500), (700,-100), (-200,-400),
(400,300), (-600,100), (250,-350), (-150,450),
(350,-250), (-450,-150), (550,50), (-750,250),
(125,-225), (-275,375), (625,-475), (-50,150)
```

produces, in stream order,

```text
(1225,425), (6775,-2125), (125,1375), (-625,425),
(1600,384), (1600,-1384), (-1188,250), (1288,250),
(1603,93), (1455,187), (2766,-230), (-1124,650),
(254,1598), (-782,560), (-543,-35), (1571,777)
```

The simulator and both accepted RTL paths agree with this independent staged
oracle. Backpressure holds the visible payload stable, and reset clears all
four stages so partial pre-reset work cannot cross the reset epoch. Artifacts
publish four distinct initialized-ROM companions of exact depths 8, 4, 2, and
1.

The target result remains a generic implementation with unknown whole-module
timing. Framing, automatic target planning, physical DSP/BRAM selection,
constant-twiddle strength reduction, synthesis QoR, and Fmax claims remain
outside this bounded reference. The complete functional FFT512 hierarchy is
documented below.

## FFT32 five-stage reference

`FFT32SDFReference` composes `D=16`, `D=8`, `D=4`, `D=2`, and `D=1`. Its
five-bit DIF bit-reversed bin order is:

```text
0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30,
1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31
```

The independently evaluated depth-16 `fixed<16,14>` twiddle image is:

```text
(16384,0), (16069,-3196), (15137,-6270), (13623,-9102),
(11585,-11585), (9102,-13623), (6270,-15137), (3196,-16069),
(0,-16384), (-3196,-16069), (-6270,-15137), (-9102,-13623),
(-11585,-11585), (-13623,-9102), (-15137,-6270), (-16069,-3196)
```

The table was rounded independently at high precision before comparison with
the source-generated ROM. The full numerical oracle applies the same
nearest-even, saturating `fixed<18,16>` `Q` boundary to each sum and difference
and again after each exact complex twiddle product, at spans `32`, `16`, `8`,
`4`, and `2`.

For `n` from 0 through 31, the deterministic raw fixture is
`(((211*n + 37) % 2000) - 1000, ((157*n + 91) % 1800) - 900)`. Explicitly:

```text
(-963,-809), (-752,-652), (-541,-495), (-330,-338),
(-119,-181), (92,-24), (303,133), (514,290),
(725,447), (936,604), (-853,761), (-642,-882),
(-431,-725), (-220,-568), (-9,-411), (202,-254),
(413,-97), (624,60), (835,217), (-954,374),
(-743,531), (-532,688), (-321,845), (-110,-798),
(101,-641), (312,-484), (523,-327), (734,-170),
(945,-13), (-844,144), (-633,301), (-422,458)
```

Its exact staged output stream is:

```text
(-2160,-2016), (624,1088), (1712,-3136), (-464,-1888),
(1734,-2219), (-1910,-453), (-2499,-970), (5171,794),
(-1170,-509), (1126,-2475), (-3785,3228), (2677,2412),
(-3264,-1291), (-1132,-2105), (1349,-1496), (-6809,-3460),
(-2129,-1656), (-191,634), (-1301,1336), (4101,-3534),
(881,-4171), (35,-2075), (-2500,-232), (-1200,-990),
(-16857,2892), (-1059,2138), (1051,-2678), (-2675,-576),
(686,3488), (998,-1386), (-197,-3437), (-1659,-1145)
```

With continuous ready/valid, the first result appears 36 cycles after `x0` and
all 32 results transfer at II=1 on relative cycles 36 through 67. A finite
frame requires 31 accepted following sentinel tokens and then five idle drain
cycles. Stalls preserve the visible payload and propagate backpressure through
all five stages; reset clears partial work in every stage. Artifacts preserve
five distinct ROM companions of exact depths 16, 8, 4, 2, and 1.

On the validation host, direct semantic compilation took about 0.19 seconds
and one bounded 72-cycle hierarchical simulation took about 7.09 seconds. This
is measurable scaling cost, not a correctness failure. The target result is
still generic with unknown whole-module timing. This FFT32 result makes no
automatic-planner, resource-mapping, synthesis-QoR, or Fmax conclusion; the
complete functional FFT512 hierarchy follows.

## FFT512 nine-stage functional reference

`FFT512SDFReference` composes the existing numerical stage at exact depths
`256, 128, 64, 32, 16, 8, 4, 2, 1`. The nine specializations, physical
instances, recursive state paths, and initialized-ROM companions remain
distinct. No new framing, planner, storage-selection, or backend-specific FFT
semantics are introduced.

The depth-256 twiddle image is generated independently with 100-digit Decimal
trigonometry and nearest-even Q2.14 conversion. Its canonical SHA-256 digest is:

```text
e5d531425e935a1a30baedfc0aecb476236686320cb0cb763309ae2ef16bb2bb
```

The complete independent integer oracle applies the accepted
nearest-even/saturating stage boundary at spans `512` through `2`. Its 512 raw
complex outputs have canonical digest:

```text
deb8344501a1976845dc3ac201ceaad1fced353d24187af9f008207cc9b73113
```

Sequential output positions are nine-bit bit reversals. Selected frozen points
are:

```text
position  bin  raw output
0         0    (1120,-3696)
1         256  (1984,3008)
2         128  (-2408,-2376)
3         384  (2376,-2408)
7         448  (3272,-5118)
15        480  (-2874,-3156)
31        496  (-1631,-2525)
63        504  (-595,-2456)
127       508  (735,-5939)
255       510  (-422,4028)
256       1    (-1186,-3789)
383       509  (509,-13653)
511       511  (711,-1067)
```

Under continuous valid/ready, the functional schedule is latency 520 and
II=1. A finite 512-token fixture needs 511 subsequently accepted ordinary
sentinel tokens, followed by nine idle drain cycles. The sentinel tokens are
part of the following continuous frame; they are not a flush command.

Current direct SystemVerilog emission produces 202,856 bytes and 1,275 lines of RTL and
passes strict Verilator lint. The real Clash 1.11 structural generation/lint
gate also passes, taking approximately 198 seconds and 1.28 GiB peak memory on
the validation host. A complete dual-backend Verilator test matches all 512
outputs against the independent oracle. It also resets a partial stream and
holds the first clean-epoch output stable through five cycles of backpressure;
the complete post-reset stream remains lossless and ordered. This combined
build/simulation takes about 355 seconds and peaks at roughly 4.61 GiB RSS.

The persistent backend-independent hierarchy simulator now completes the same
1,033-cycle continuous replay routinely. On the validation host it takes about
11 seconds and peaks at roughly 84 MiB RSS. The replay accepts all 1,023 input
transfers (the 512 fixture samples followed by 511 ordinary sentinel samples),
produces exactly 512 outputs, and matches the frozen digest and latency window
above. It runs in the default regression with a 60-second hard timeout rather
than behind an opt-in gate.

The backend-independent simulator, direct-SystemVerilog/Verilator, and real
Clash 1.11/Verilator paths therefore agree with the same independent frozen
oracle. This closes the former simulator-scalability limitation without adding
an FFT-specific compiler path or changing the functional schedule.
