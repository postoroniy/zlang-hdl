# Types and numerics

ZLang types describe hardware representation exactly. Width, signedness,
fixed-point scale, vector length, struct identity, and fixed-point overflow
policy participate in typing and canonical identity.

## Characters, fixed strings, and tuples

`char` is a source alias for canonical `u8`; it is intentionally not a separate
nominal type. `string<N>` is a source alias for the corresponding fixed hardware
collection and normalizes to canonical `vec<N,u8>`:

```zlang
letter : char = 'A'
tag : string<4> = "OFDM"
joined = concat("802.", "11a")
```

Literals contain printable ASCII directly and may use `\0`, `\n`, `\r`,
`\t`, `\\`, `\'`, `\"`, or an exact `\xNN` byte. Strings have no hidden
terminator or length field and cannot be empty. They inherit vector indexing,
equality, `length`, `generate`/`map`, homogeneous concatenation, pure functions,
generic specialization, and typed compile-time constant parameters. Registers,
FIFOs, synchronous memories, and ROMs store them with the same semantics as
`vec<N,u8>`. When packed, the first character is the most-significant byte
(`pack("AB") == 0x4142`). Unicode, interpolation, formatting, padding, and
dynamic-length strings are not implicit operations.

Tuples are structural ordered aggregates. Tuple types use `(T,U)` and tuple
values use the same positional spelling:

```zlang
pair : (u8, bit) = (data, last)
payload = pair[0]
(next_data, next_last) = pair
```

Tuple arity is 2 through 8. Projection is a zero-based integer literal and flat
destructuring introduces exhaustive immutable bindings. Nested tuple values are
legal, but nested/wildcard patterns, runtime tuple selection, tuple update,
arithmetic, and ordering are not. Exact `==`/`!=` is structural. Component zero
occupies the most-significant packed region; `pack`/`bitcast` require every
component to satisfy the ordinary non-enum packing rules.

`char` is indistinguishable from `u8` during overload resolution, and
`string<N>` is indistinguishable from `vec<N,u8>`. Tuple identity, by contrast,
is the recursive ordered sequence of component types.

## Nominal enums

Enums give control state a nominal hardware type without exposing a numeric
encoding in source:

```zlang
enum State { Idle Header Payload Done }

reg state : State = State.Idle
active = state == State.Payload
```

Members use qualified names. Their declaration-order ordinals are encoded in
`max(1, ceil_log2(member_count))` bits, so a one-member enum still occupies one
bit. The encoding is a stable storage/backend ABI, but enum values are not
integers: arithmetic, ordering, resizing, and cross-enum comparison are errors.
Equality and inequality require the same nominal enum declaration.

When an external encoding is part of the hardware contract, the representation
can instead be declared explicitly:

```zlang
enum WifiRate : bits<3> {
    Continue = 0
    Bpsk6 = 1
    Qpsk12 = 2
    Qam16_24 = 4
}
```

Every member then requires one unique code fitting the exact `bits<W>` backing
type. Declaration order still controls exhaustive source selection, while the
declared sparse code is the physical representation. Mixing explicit and
implicit members is an error. The ordinal form above remains unchanged.

Raw boundaries use total, typed operations rather than enum casts:

```zlang
raw     : bits<3> = enum_encode(rate)
valid   : bit = enum_valid<WifiRate>(raw)
decoded : WifiRate =
    enum_decode<WifiRate>(raw, WifiRate.Continue)
```

`enum_encode` is lossless. `enum_valid<T>` recognizes exactly the declared
codes. `enum_decode<T>` returns its required same-enum fallback for every hole
in the encoding, so invalid raw bits never become an invalid nominal value.
These operations perform no resize and accept no signed/integer substitute.

Enums may appear in locals, outputs, registers/rules, structs, and vectors. A
top-level enum input is intentionally rejected in this first slice because an
external invalid bit pattern would violate the nominal value invariant. General
`unpack<Enum>` is likewise deferred. Exhaustive selection is described under
[operators and selection](expressions-functions-generics.md#operators-and-selection).

## Nominal tagged unions

A tagged union represents one of several named payload shapes without exposing
raw tag arithmetic:

```zlang
union Message {
    Idle
    Data { value : u8 }
    Error { code : bits<4> }
}

message : Message = Message.Data { value = input }
result : u8 = match message {
    Message.Idle => 0
    Message.Data { value } => value
    Message.Error { code } => extend<8>(code)
}
```

The nominal declaration identity is part of the type. The source-order tag is
stored in the most-significant bits; fields occupy the maximum-width payload in
source order and shorter payloads receive zero low-order padding. A fieldless
constructor is written `Message.Idle`. Every pure `match` is exhaustive, names
each variant once, and binds exactly the declared fields. See
[the complete bounded surface](tagged-unions.md).

## Scalar families

| Spelling | Meaning |
| --- | --- |
| `bit` | A one-bit control value, distinct from integers and raw bits. |
| `uN`, `uint<N>` | An `N`-bit unsigned integer. |
| `sN`, `sint<N>` | An `N`-bit two's-complement signed integer. |
| `bits<N>` | An `N`-bit raw vector without integer arithmetic. |
| `fixed<W,F>` | Signed fixed point, `W` total bits and `F` fractional bits, wrapping target narrowing. |
| `ufixed<W,F>` | Unsigned fixed point with wrapping target narrowing. |
| `fixed_sat<W,F>` | Signed fixed point with saturating target narrowing. |
| `ufixed_sat<W,F>` | Unsigned fixed point with saturating target narrowing. |

Widths are positive compile-time integers. Fixed-point types additionally
require `0 <= F < W`. The families are distinct: no implicit signed/unsigned,
integer/raw-bit, or integer/fixed conversion is performed.

Aliases disappear during typing:

```zlang
type Address = uint<32>
type Payload = bits<64>
```

Alias cycles, duplicate declarations, and redefinition of built-ins are errors.

## Integer width rules

An unsuffixed integer literal has the smallest exact hardware type implied by
its value. Non-negative literals are unsigned; syntactic negative literals are
signed two's-complement values:

| Literal | Inferred type |
| --- | --- |
| `0`, `1` | `u1` |
| `2`, `3` | `u2` |
| `255` | `u8` |
| `-1` | `s1` |
| `-2` | `s2` |
| `-3`, `-4` | `s3` |
| `-123` | `s8` |

Decimal, hexadecimal, and binary spellings with the same value infer the same
type; leading zeros do not request storage width. A direct literal at an
explicitly typed assignment, field, argument, or return boundary instead uses
that target type when the value fits exactly. An out-of-range literal is an
error. Context never recursively resizes a compound expression, so
`concat(0,1)` and `0 + 1` retain the types derived by their own operators.

Unary `-` applied to a non-literal remains ordinary typed arithmetic. It does
not reinterpret an unsigned signal as a signed one.

| Expression | Result |
| --- | --- |
| `uN + uM` | `u(max(N,M)+1)` |
| `sN + sM` | `s(max(N,M)+1)` |
| `uN - uM` | `u(max(N,M))`, modular underflow |
| `sN - sM` | `s(max(N,M)+1)` |
| same-family `N * M` | Width `N+M` |
| `&`, `\|`, `^` | Matching family, widest operand width |
| shifts | The left operand's exact type and width |
| comparisons | `bit` |

Addition and multiplication preserve carry bits. A left shift discards bits
beyond its fixed result width. Signed right shift is arithmetic; unsigned and
raw-bit right shift is logical.

Use explicit resizing:

```zlang
wide = extend<17>(value)
low  = truncate<8>(wide)
```

`extend<N>` preserves signedness behavior and `truncate<N>` keeps low bits.
Neither changes the type family.

At an explicitly typed output, local, register write, storage write, child
binding, struct field, or declared function-return boundary, the width may be
obtained from that boundary while the conversion remains explicit:

```zlang
low : u8 = truncate(wide)
wide_again : u17 = extend(low)
```

The contextual and explicit-width forms normalize to the same typed resize.
An inferred local, a nested arithmetic operand, or a mismatched type family has
no such context and must keep `truncate<N>` or `extend<N>`.

## Fixed point

Concrete spellings expose integer and fractional widths directly:

| Spelling | Canonical type | Narrowing policy |
| --- | --- | --- |
| `SF8.8` | `fixed<16,8>` | wrap |
| `SF_Sat8.8` | `fixed_sat<16,8>` | saturate |
| `UF8.8` | `ufixed<16,8>` | wrap |
| `UF_Sat8.8` | `ufixed_sat<16,8>` | saturate |

For signed values the integer part includes the sign bit. `SF8.8` therefore
represents `-128.0` through `127.99609375` in steps of `2^-8`.

Arithmetic is widened before any storage conversion. Fixed addition and
subtraction require equal fractional widths; multiplication returns total width
`W1+W2` and fractional width `F1+F2`. Target overflow policy applies only at an
explicit or contextual storage conversion, not after every intermediate node.

Direct fixed-point literals must be exactly representable and in range, even
for a saturating target. Loss is explicit:

```zlang
y = quantize<SF_Sat8.8>(value) {
    round nearest_even
    overflow saturate
}
```

Supported rounding modes are `toward_zero`, `floor`, `away_zero`, and
`nearest_even`. Conversion order is exact rescale, rounding, then overflow.
`fixed_raw(0x0C22)` creates a target-context raw pattern; `fixed_to_raw` exposes
the stored integer.

An exact fixed-point `dot(a,b)` retains the full accumulator. If the destination
reduces fractional precision, give one post-accumulation rounding mode:

```zlang
out y : SF_Sat8.8
y = dot(coefficients, samples, floor)
```

## Vectors and structs

`vec<N,T>` has a compile-time length:

```zlang
in samples : vec<4,u8>
```

An explicitly typed boundary can use a non-empty vector literal or contextual
replication:

```zlang
pair    : vec<2,u8> = [left, right]
zeros   : vec<4,u8> = repeat(0)
zeros_2 : vec<4,u8> = repeat<4>(0)
```

Every element must have the exact contextual element type. `repeat(value)`
requires the destination vector to provide its length; `repeat<N>(value)` makes
the length explicit. Both forms retain ordinary vector sequence order and lower
to the existing typed generation representation.

Constant indexes are checked during elaboration. Runtime reads are accepted only
when conservative range analysis proves every encoded value in range; ZLang
does not silently mask or clamp. A rule may update one range-proven element of
a one-dimensional `reg vec<N,T>` atomically. Nested paths, multiple dynamic
writes to the same register action group, and runtime-selected instances remain
unsupported; see [Sequential logic, rules, and storage](sequential-state-storage.md#runtime-indexed-vector-register-updates).

Structs are nominal products:

```zlang
struct Packet {
    data : u64
    last : bit
}

packet = Packet { data = payload last }
```

Constructors are complete and may use same-name field punning. An immutable
update rebuilds the same nominal struct and preserves fields not named in the
update:

```zlang
completed = packet with { last = 1 }
```

Unknown or duplicate fields and values of the wrong exact field type are
rejected. This is a pure value expression, not mutation. Recursive layouts are
still rejected. Generic structs are covered in
[Expressions, functions, and generics](expressions-functions-generics.md).

Structural `==` and `!=` are available for exact matching struct and vector
types. Comparison is recursive over every field/element and returns `bit`; it
does not perform conversion, resizing, or nominal coercion.

An immutable nominal struct can also be destructured exhaustively:

```zlang
Packet { data, last } = packet
```

Every declared field must appear exactly once, under its declared name, and the
new immutable bindings may not shadow another symbol. Destructuring lowers to
ordinary typed field values; it does not add tuple patterns, partial patterns,
renaming, mutation, or runtime matching.

## Slicing, concatenation, and representation

An inclusive, compile-time bit slice returns raw bits:

```zlang
in word : u16
out byte : bits<8>
byte = word[15:8]
```

Both `MSB` and `LSB` are compile-time integer expressions. The result is
`bits<MSB-LSB+1>`. Reversed, negative, runtime, or out-of-range bounds are
errors; slicing never resizes, reinterprets, or clamps its operand.

A `bits<N>` value also supports concise single-bit selection:

```zlang
in raw : bits<24>
out lsb : bit = raw[0]
out msb : bit = raw[23]
```

Packed indices use conventional bit numbering: index zero is the least-
significant bit, exactly as `[0:0]`. The selector must currently resolve at
compile time, including inside `generate`; a runtime selector is rejected
instead of being silently converted into a mux. Vector indexing is different:
`vector[0]` is the first logical element and occupies the MSB packed region.

`concat(a,b,...)` accepts at least two operands. When every operand is a vector
with the exact same element type it returns one longer vector, preserving source
and element order:

```zlang
in left  : vec<2,u8>
in right : vec<3,u8>
out all  : vec<5,u8>
all = concat(left, right) // left[0], left[1], right[0], right[1], right[2]
```

Otherwise no operand may be a vector and all operands must be recursively
bit-packable. The result is raw `bits<N>` and the first operand occupies the
most-significant bits:

```zlang
joined = concat(high, low) // high is the MSB portion
```

Each scalar operand is typed independently, before the result width is known.
For example, `concat(0,1)` is `bits<2>` and `concat(3,0)` is `bits<3>`.
Neither an assignment target nor a function caller may widen an operand or the
completed concatenation implicitly.

Exact packed fill constants are available without manufacturing a numeric
literal of the desired width:

```zlang
clear_mask : bits<24> = zeros<24>
set_mask   : bits<24> = ones<24>
```

The width is a positive compile-time integer expression. Both forms produce
exact `bits<N>` values and lower to ordinary constants; they are not vector
replication. The separate `zero<x>`/`ones<x>` spellings inside an `equiv`
declaration remain typed pattern constants rather than hardware expressions.

Vector/scalar mixtures and different vector element types are errors. Convert a
vector explicitly with `bitcast<bits<W>>` when raw bit assembly is intended.

`reshape(value)` obtains a vector target shape from an explicitly typed context;
`reshape<T>(value)` supplies it directly. Reshape recursively enumerates leaves
outer-to-inner, then rebuilds the requested shape without packing them:

```zlang
in matrix : vec<2,vec<4,u8>>
out flat  : vec<8,u8>
flat = reshape(matrix)
matrix_again = reshape<vec<2,vec<4,u8>>>(flat)
```

Source and target must be vectors with the same leaf count and exact same leaf
type. Reshape has no endianness and performs no numerical conversion.

`bitcast<T>(value)` exposes or reconstructs the exact stored representation
when source and target have equal packed width and both are recursively
bit-packable and non-enum. It never resizes, sign-extends, truncates, rescales,
rounds, or saturates:

```zlang
raw    : bits<8> = bitcast<bits<8>>(signed_value)
signed : s8      = bitcast<s8>(raw)
```

`pack(value)` and `unpack<T>(raw)` remain supported low-level compatibility
spellings and normalize to the same typed `Bitcast` operation. Signed integers
and signed fixed-point values use their stored two's-complement pattern.

Aggregate layout is deterministic and backend-independent:

- struct field zero (declaration order) is at the MSB;
- vector element zero is at the MSB;
- tuple component `item0` is at the MSB;
- nested structs, vectors, and structural tuples apply the same rule recursively.

The initialized `rom<T,N>` image format uses this same layout for each word.
Images contain one exact-width binary word per line with address zero first;
they do not inherit host byte order. Consequently simulator values and
direct-SV `$readmemb` agree on nested aggregate and fixed-point ROM contents.
See [Initialized synchronous ROMs](sequential-state-storage.md#initialized-synchronous-roms).

Scalar numeric/raw types and recursively bit-packable structs, vectors, and
structural tuples are accepted. Nominal enums, including an aggregate containing
an enum, are intentionally rejected by `bitcast`, `pack`, and `unpack`. Enum
ordinal storage remains available through ordinary typed values and `reshape`,
but raw construction could create an invalid ordinal and therefore requires a
future reviewed validity policy.

At explicitly typed assignment/storage boundaries, ZLang may insert an
equal-width raw bitcast only when either source or target is `bits<N>` or flat
`vec<N,bit>`. This makes raw wiring concise while preserving the rule that
direct signed-to-unsigned, integer-to-fixed, resizing, and rescaling conversions
are never implicit. Raw conversion does not participate in function arguments,
operator overloads, generic inference, switch branch unification, or protocol
compatibility.

Scalar `reduce(&, value)`, `reduce(|, value)`, and `reduce(^, value)` reduce the
stored bits of `bit`, `bits<N>`, `uN`, or `sN` and return `bit`. `parity(value)`
is the concise XOR form and also accepts `vec<N,bit>`. Existing vector reduction
still reduces elements and returns the element type; fixed-point and struct
scalars are not representation-bit reduction operands.

## Current boundaries

Runtime packed-bit selection, runtime/reversed/out-of-range slicing, zero or
unresolved packed widths, one-operand or heterogeneous-vector concatenation,
runtime reshape,
unequal-width bitcast, enum bitcast, and external top enum inputs remain
deliberately rejected. These boundaries are diagnosed rather than inferred by
a backend. See the
[syntax support matrix](syntax-support-matrix.md).
