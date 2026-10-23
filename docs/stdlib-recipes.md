# Standard library recipes

Use the API inventory to find an import and exact signature, then read its
component contract before choosing widths and buffering. The examples below
are ordinary source programs, not compiler-recognized application names.

## Stream composition and CRC

Start with [the framed-CRC example](../examples/framed_crc.zhl).
`RvFork2` delivers each accepted payload exactly once to each branch;
`RvJoin2` pairs accepted elements by sequence, not by the cycle when they arrive.
Connect one clock/reset domain and keep output payload stable under backpressure.
The [stream/CRC contract](stdlib-stream-dsp.md) specifies simultaneous
retire/refill and CRC initialization, polynomial and final XOR.

## Choosing a FIR

Choose immutable parallel FIR for II=1; choose the explicit folded datapath
when one logical MAC and II=N match the application. One logical multiplier
is not a promise of one DSP for arbitrary widths. Reloadable wrappers use
two banks and an explicit atomic commit; do not update live coefficients.
See [FIR contracts](stdlib-reloadable-fir.md) and the examples linked there.

Exact cores retain full product/reduction precision. Quantized wrappers apply
one final nearest-even/saturating conversion. Do not insert conversions between
products unless the application intentionally changes its numerical contract.

## Changing the sample rate or delay

Use polyphase interpolation/decimation for a supplied FIR prototype and fixed
integer rate. CIC uses an explicitly chosen modular internal width and no
hidden gain correction. Halfband requires checked symmetric coefficients and
alternating zeros. [Multirate contracts](stdlib-multirate-coding.md) define
coefficient order and accepted-sample timing.

Farrow changes fractional delay, not the input sampling schedule. Each beat
carries its own `mu_raw`; [the Farrow contract](stdlib-farrow.md) distinguishes
stored-coefficient exactness, ideal-filter approximation and final rounding.

## Choosing an error-control component

- [SECDED](stdlib-secded.md): correct one bit, detect every two-bit error.
- [DVB RS](stdlib-rs-dvb.md): the specified shortened RS(204,188) byte order;
  the errors-only decoder corrects up to eight symbol errors, not erasures.
- [Viterbi](stdlib-viterbi.md): minimum-metric framed K=7 decoding compatible
  with the existing encoder; a successful decode is not an error-detection guarantee.

Do not interchange the DVB polynomial/root/shortening conventions with CCSDS
or another RS profile. Keep frame markers explicit; do not infer them from data.

## Reproducing an example

From a prepared checkout, check a linked source, select the documented concrete
top, generate Direct-SV and run the accompanying event schedule. The [User Guide](user-guide.md)
explains `make venv`, JSONL event units, native simulation and waveform viewing.
The [Language Reference](language-reference.md) owns the common ready/valid,
exact-width and clock/reset semantics; component chapters add only their own contracts.
