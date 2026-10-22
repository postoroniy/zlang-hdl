# Farrow fractional delay

`std.dsp.farrow` provides `RvFarrowFixedExact`,
`RvFarrowFixedNearestEvenSaturate`, `RvFarrowLagrangeLinear` and
`RvFarrowLagrangeCubic`. These are fractional-delay filters, not complete
sample-rate converters: the caller supplies each sample and its delay value.

Input payload is `FarrowSample<SW,SF,MF>`:

- `sample:fixed<SW,SF>`;
- `mu_raw:uint<MF>`, representing μ=mu_raw/2^MF, with 0≤μ<1.

The exact core admits M=0…3, K=1…8 and MF≥1. The immutable matrix has M+1
rows of K `fixed<CW,CF>` coefficients. Row j computes
`v_j = sum(c[j,k]*x[n-k])`; Horner evaluation computes
`v_0 + μ*(v_1 + … + μ*v_M)`. Tap zero uses the current accepted sample.
History changes only on input transfer.

With B=SW+CW+ceil_log2(K) and F=SF+CF, the exact output is
`fixed<B+M*(MF+2),F+M*MF>`. Subfilter products/sums and every Horner stage
retain their full typed widths. Scale alignment appends zero raw bits;
it does not discard fractional information. Exactness refers to the stored
binary coefficients, not an ideal rational filter.

There is one registered subfilter stage followed by M registered Horner
stages: unstalled latency=M+1 edges, II=1. Samples, μ and validity move
together. Output backpressure freezes the entire pipeline. Input is accepted
when `input.valid && input.ready`; reset clears history and all in-flight
validity. The final-quantized variant applies nearest-even/saturation once,
after the last register, without another stage.

## Lagrange profiles

The convenience wrappers use immutable `fixed<18,16>` coefficient matrices.
`farrow_lagrange_linear_q16()` has exact coefficients for delay μ.
`farrow_lagrange_cubic_q16()` stores nearest-even binary approximations of
the cubic Lagrange coefficients for delay 1+μ. That one-sample algorithmic
offset is separate from the four-edge pipeline latency.

`RvFarrowLagrangeLinear<SW,SF,MF>` returns
`fixed<SW+MF+21,SF+MF+16>`; the cubic wrapper returns
`fixed<SW+3*MF+26,SF+3*MF+16>`. For sample fixed<16,14>, MF=16, cubic exact
output is fixed<90,78>. For a smaller output, pass the coefficient helper to
`RvFarrowFixedNearestEvenSaturate` with explicit OW/OF, as in the example.

For arbitrary fixed<16,14> input samples and any representable μ, the cubic
binary-coefficient approximation differs from the ideal cubic Lagrange
polynomial by at most 1/24576 in sample units. This follows from the exact
rational coefficient-error sum 1/49152 and |sample|≤2; it is a conservative
uniform bound, not a correct-rounding claim. A final fixed<16,14>
nearest-even/saturating conversion adds at most half an output LSB relative
to the clipped exact result. Error relative to an *unclipped* ideal result
also includes any intentional saturation. No all-band ideal-delay accuracy,
SFDR or sample-rate-controller guarantee follows from this certificate.

## Run

```sh
.venv/bin/zlang sim examples/farrow.zhl --top FarrowCubic \
  --events examples/farrow.events.jsonl --trace build/farrow.fst --json
```

The JSONL sample integers are raw fixed-point values: 16384 means 1.0 at
SF=14. `mu_raw=32768` means μ=0.5 at MF=16. The example changes μ per
accepted sample, stalls the first result, then drains the pipeline. Only
output transfers count as delivered results. Coefficient reload, filter
design and resampling control are outside this module.
