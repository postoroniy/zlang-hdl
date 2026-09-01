# Fixed-point types

ZLang fixed point is a scaled integer type in backend-independent semantic IR.
Simulator, Clash, and direct SystemVerilog consume the same typed conversions;
no backend defines separate numeric rules.

## Formats

`SFI.F` and `UFI.F` are wrap formats. `SF_SatI.F` and `UF_SatI.F` saturate
when an exact same-scale result is stored at a narrower width. `I` is positive,
`F` may be zero, and total storage width is `I + F`. The signed integer field
includes its sign bit.

```zlang
in sample  : SF8.8
in gain    : UF2.14
out result : SF_Sat8.8
```

The parameterized equivalents are `fixed<W,F>`, `ufixed<W,F>`,
`fixed_sat<W,F>`, and `ufixed_sat<W,F>`, where `W` is total storage width.
For example, `SF8.8` and `fixed<16,8>` are one canonical type.

## Arithmetic and conversion

Addition and subtraction require equal signedness and fractional width and
produce a widened exact result. Multiplication produces `W1+W2` storage bits
and `F1+F2` fractional bits. Comparisons require compatible signedness and
scale.

Same-scale assignment may widen or narrow. A wrap target discards high bits; a
saturating target clamps to its signed or unsigned minimum/maximum. This policy
applies at typed storage boundaries, including outputs, locals, register and
rule updates, function results, struct fields, and child bindings.

Decimal literals are parsed as exact rationals. Direct assignment accepts a
literal only when it is exactly representable and in range; it never silently
rounds, wraps, or saturates. For example, `0.5` is exact in `SF8.8`, while `0.1`
and `1000.0` are errors. Loss is always visible at the source boundary:

```zlang
out y : SF_Sat8.8
y = quantize(1000.0, floor) // clamp after rounding
```

Changing fractional width is never implicit. `quantize` supports
`toward_zero`, `floor`, `away_zero`, and `nearest_even`, followed by either
`wrap` or `saturate`. The destination supplies overflow policy in the concise
form; the full form can override both policies:

```zlang
y = quantize<SF8.8>(wide) {
    round nearest_even
    overflow saturate
}
```

The compatibility intrinsics `fixed_truncate_wrap`,
`fixed_truncate_saturate`, `fixed_round_even_wrap`, and
`fixed_round_even_saturate` remain supported. Their truncate behavior is
`toward_zero`.

`fixed_raw(pattern)` constructs the target fixed value from one unsigned raw
bit pattern. The target supplies width and signed interpretation, and its
overflow policy is deliberately ignored. Thus `fixed_raw(0xFFFF)` assigned to
`SF_Sat8.8` is raw `-1`, representing `-1/256`; it does not saturate.
`fixed_to_raw` exposes the stored scaled integer.

For fixed vectors, `dot(a,b)` retains full product and accumulator precision.
If an explicitly declared target discards fractional bits, specify rounding:

```zlang
out y : SF_Sat8.8
y = dot(coeff, samples, floor)
```

This lowers to one conversion after the exact reduction, never one conversion
per product.

### Rounding modes

| Mode | Result |
| --- | --- |
| `toward_zero` | Discard the fraction toward zero. Signed negative values need correction after a bit slice. |
| `floor` | Round toward negative infinity. This is a logical/arithmetic shift or slice. |
| `away_zero` | If any discarded bit is set, increase the magnitude. |
| `nearest_even` | Round to nearest; an exact midpoint chooses an even target LSB. |

The canonical conversion order is exact rescale, selected rounding, then
selected overflow. Saturation never selects a rounding mode implicitly.

## Library and limits

`std.math.fixed` contains ordinary source-authored helpers for absolute value,
clamp, min/max, and full-precision MAC. The polyphase FIR example uses concise
formats for samples, coefficients, pipeline stages, and results.

There is no implicit signed/unsigned conversion, automatic rescaling, binary
floating-point evaluation of decimal literals, or backend-specific saturation
primitive. General fixed-point division and fixed-point e-graph rewrites remain
unsupported.

The manual four-architecture FIR and routed vendor comparison are documented in
`docs/fixed-fir-architecture-validation.md`. Automatic fixed exploration remains
disabled pending broader device/vendor evidence.
