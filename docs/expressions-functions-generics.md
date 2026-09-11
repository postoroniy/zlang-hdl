# Expressions, functions, and generics

ZLang expressions are pure typed hardware values unless they explicitly contain
a timing construct. Assignment requires an exact canonical target type.

## Operators and selection

From highest to lowest precedence:

1. calls, parentheses, aggregate selection, conversions, functional forms;
2. unary `-` and logical `!`;
3. `*`, and compile-time `/`;
4. `+`, binary `-`;
5. `<<`, `>>`;
6. `&`, then `^`, then `|`;
7. `==`, `!=`, `<`, `<=`, `>`, `>=`;
8. `&&`, then `||` in compile-time/equivalence guards;
9. right-associative `condition ? true_value : false_value`.

Unary `-` is implemented for numeric scalar/fixed types and may be overloaded
for a nominal struct. It is not a general implicit-cast mechanism. Runtime
logical `!` accepts exactly `bit` and returns `bit`; it is equivalent to an
exact comparison with zero. `/`, `&&`, and `||` are available only where the
compile-time/equivalence sublanguage accepts them; they do not infer runtime
divider or general truthiness for multi-bit hardware values.

Two-way runtime selection can use `?:` or `mux`:

```zlang
y = select ? a : b
y = mux(select, a, b)
```

The condition is `bit` and both arms have one exact type. Numeric `switch`
requires unique fitting keys and an `else` arm:

```zlang
y = switch opcode {
    0 => a
    1 => b
    else => 0
}
```

A switch over a nominal enum uses qualified members and must cover every member.
It needs no catch-all arm, which keeps a newly added state from silently taking
an old default path:

```zlang
next = switch state {
    State.Idle => State.Header
    State.Header => State.Payload
    State.Payload => State.Done
    State.Done => State.Idle
}
```

Numeric keys in an enum switch, members from another enum, duplicate members,
and incomplete coverage are errors. Numeric switches retain their existing
mandatory `else` behavior.

Nominal tagged unions use the parallel exhaustive `match` expression. Each arm
names its union and variant and binds the exact source-declared payload names;
there is no default arm or hidden priority:

```zlang
y = match message {
    Message.Idle => 0
    Message.Data { value } => value
}
```

Typing lowers this surface to an ordinary exact-width `switch` over the union
tag plus typed field projections before canonical optimization and backend
emission.

Exact matching structs, vectors, and structural tuples also support `==` and
`!=`. These comparisons recursively combine the ordinary exact leaf
comparisons and return `bit`; mismatched nominal structs, vector lengths,
tuple component order/arity, or element types are errors.

## Aggregate value ergonomics

Vector literals and replication use their explicit destination type:

```zlang
pair  : vec<2,u8> = [left, right]
zeros : vec<4,u8> = repeat(0)
```

`repeat<N>(value)` may state the compile-time length explicitly. The contextual
and explicit forms lower to the same existing vector-generation semantics.
Struct update is likewise a pure expression over an existing nominal value:

```zlang
next = current with { valid = 1 payload = replacement }
```

It preserves omitted fields and lowers to a complete typed struct construction;
it does not create mutable records or a backend-specific update operation.
Exhaustive `Struct { field, ... } = value` destructuring similarly lowers to
ordinary immutable field bindings. It requires every field exactly once and
does not provide partial patterns, renaming, or runtime pattern matching.
Flat tuple destructuring `(first, second) = pair` likewise evaluates one RHS
and introduces fresh immutable projections. Nested, wildcard, and rest patterns
remain outside this bounded surface.

## Pure functions

Functions are top-level, pure, and combinational. The smallest body is one
result expression:

```zlang
fn tap(sample : u8, coefficient : u8) -> u16 {
    sample * coefficient
}
```

They may call other non-recursive pure functions. They cannot capture module
ports or contain state, clocks, delays, protocols, or pipelines. Return types
may be explicit or inferred for both ordinary and generic functions:

```zlang
fn make_tag(high : bits<2>) {
    concat(high, zeros<2>, ones<2>)
}
```

An inferred body is checked without a caller-provided expected type, and its
exact final-expression type becomes the function signature. Forward references
are resolved independent of declaration order. Direct or indirect inferred
return cycles are rejected; ZLang does not attempt a recursive type fixed
point. For ordinary functions, explicit and inferred spellings with the same
exact signature lower to the same callable identity. Generic specialization
identity continues to include the generic declaration form and its exact
specialization arguments.

A body may introduce sequential immutable bindings without `let`, `const`, or
`return`; the final expression is the result:

```zlang
struct Pair<type T> { left : T right : T }

fn duplicate_sum<type T>(a : T, b : T) {
    exact = a + b
    Pair { left = exact right = exact }
}
```

Each binding has its exact inferred type, may reference parameters and earlier
bindings, and cannot shadow or be reassigned. Forward references and module
captures are rejected. This syntax is normalized into the same concrete typed
expression/call IR as the nested expression spelling.

Source units are declaration-order independent: functions, structs, enums, and
modules may be declared after their users, and a shipped library unit may
contain declarations without a hardware top. Parameterized modules can state
compile-time obligations at specialization:

```zlang
module Queue<type T,D=4>
where D >= 2 && is_power_of_two(D) {
    // ...
}
```

The constraint is checked against exact type/value arguments and creates no
runtime hardware.

### Typed constants and statically selected functions

Module and generic-function specializations may carry required named
compile-time constants and pure function references:

```zlang
fn widen(x : u8) -> u9 { extend<9>(x) }

fn transform_with<
    type A,
    type B,
    operation : fn(A) -> B
>(x : A) {
    operation(x)
}

module ImageBank<type T,N,IW,image : vec<N,T>> {
    clock clk
    reset rst
    in address : uint<IW>
    out data : T

    rom table : rom<T,N> { read_latency 1 init image }
    table.read_address = address
    data = table.read_data
}
```

These argument kinds are always named: `image=image` and
`operation=fn widen`. A generic target may be selected explicitly, for example
`operation=fn widen_as<A=u8,B=u9>`. Constants must have the exact declared,
recursively bit-packable non-enum type and must evaluate completely during
elaboration. A function reference must be pure and match the exact parameter
and return types; it is not a runtime function pointer and cannot be stored,
returned, compared, or carried through hardware ports.

Type and integer shape parameters resolve before constant/function parameters.
The compiler retains a typed binding record containing the canonical constant
value or exact callable identity, dependency closure, evaluator schema, and
content digest. Source spelling is absent from semantic, artifact, and cache
identity. Defaults for constant/function parameters are intentionally not part
of this first bounded slice.

## Compile-time functions and selection

Compile-time evaluation is bounded and deterministic. Structural intrinsics are
compiler-owned, not stdlib overloads:

- `length(vector)`;
- `floor_log2(n)`, `ceil_log2(n)`, `index_width(n)`,
  `is_power_of_two(n)`;
- `pi()`, `sin(x)`, `cos(x)`, `log2(x)`, and `log(base,x)`.

Real intrinsics use a versioned decimal evaluator rather than host binary
floating point. A non-integral real enters hardware only through explicit
fixed-point quantization.

`index_width(n)` is the storage width for an index into `n` elements:
`max(1, ceil_log2(n))`. Its argument must be a positive compile-time integer
expression. Integer parameter defaults are resolved in declaration order and
may reference only earlier parameters:

```zlang
module Table<N=8,IW=index_width(N)> { ... }
```

A forward reference such as `module Bad<IW=index_width(N),N=8>` is rejected.

Specialization-time `if` accepts value parameters and canonical type parameters:

```zlang
fn type_flag<type T>(x : T) {
    if T == u8 { 1 } else { 0 }
}

module Stage<D=1> {
    out y : u8
    if D == 1 { y = 1 } else { y = 2 }
}
```

Only the selected branch is semantically checked. Runtime signals are rejected
in compile-time conditions with a suggestion to use `?:`, `mux`, `switch`, or a
guarded action.

## Functional datapath

Ranges are half-open and compile-time bounded:

```zlang
products = generate(i in 0..8) a[i] * b[i]
doubled  = map(i in 0..8) { a[i] + a[i] }
y        = reduce(+, products)
z        = sum(i in 0..8) a[i] * b[i]
d        = dot(a, b)
```

`generate` and `map` produce `vec<N,T>`. `reduce` supports `+`, `*`, `&`, `|`,
and `^`; `sum` is addition reduction. Reduction order is a deterministic
source-order balanced tree, so finite-width intermediate types are stable across
backends. `dot` uses full-width products followed by that same reduction.

For a nominal struct element, only `sum`/`reduce(+, ...)` is defined. Every
internal node resolves the ordinary exact `operator +` overload for the two
types produced by its children; the overload result is the exact type presented
to the next node. Small and otherwise ineligible reductions retain that complete
typed tree directly. Eligible large pure bounded regions instead retain a
`FunctionalRegion` plus an `ExactReductionPlan`; the plan records the same
balanced topology, exact intermediate result types, and resolved overload/callee
identity without cloning every call body. These are two storage forms for one
language contract, not two reduction semantics.

There is no component-wise field rule, implicit conversion, identity insertion,
or special case for `Complex`. M32 and the e-graph do not reassociate the frozen
tree, and explicit quantization remains exactly where the source or overload
body placed it.

Range bounds may use resolved module value parameters. Runtime ranges, empty
value-producing ranges, and unbounded generation are rejected.

Compile-time iterator arithmetic is mathematical integer arithmetic. It does
not require hardware `extend`/`truncate` noise:

```zlang
reversed = generate(i in 0..48) bits[47 - i]
```

For a vector, `values[first..past_last]` is a half-open compile-time range and
lowers to the same ordered static element references as the corresponding
vector literal. It is not a runtime slice and does not change packed bit-slice
syntax `raw[MSB:LSB]`.

A functional iterator is a compile-time integer and may feed a generic value
specialization directly or through a bounded constant expression:

```zlang
fn coefficient<K>() -> u8 { K }

values = generate(k in 0..4) coefficient<K=k>()
next   = map(k in 0..4) { coefficient<K=k+1>() }
total  = sum(k in 0..4) coefficient<K=k>()
```

The evaluated value, not the binder spelling, participates in specialization
identity. Runtime values are not specialization constants, and an iterator
binder cannot shadow a module parameter.

### Scalable bounded functional elaboration

Large compile-time `generate`/`map` regions with a binder-invariant result type
may use the compact functional representation described above. The compiler
still type-checks the region with the ordinary exact rules and counts its
logical elements against the same bounded generation budget (currently at most
4096); it does not defer type checking to a backend or create runtime loops.
Callable specializations remain ordinary monomorphic definitions, and each use
retains its own source origin.

The accepted whole-vector IFFT64 reference exercises an outer 64-lane
`generate`, 64 exact products per lane, nominal complex reduction, and one final
quantization. N=64 is validated through semantic analysis, canonical round-trip,
and the simulator; N=8/N=16 exercise direct SystemVerilog and Verilator.
This evidence does not claim full 4096-multiplier N=64 RTL or a production
streaming architecture. See the
[802.11a validation report](80211a-transmitter-validation.md#ifft64-numerical-reference-elaboration-boundary)
for the measured boundary and the later streaming implementation.

A fully compile-time generated vector may initialize an immutable ROM without
external source generation:

```zlang
fn coefficients<N>() {
    generate(i in 0..N) truncate<8>(i * i)
}

rom table : rom<u8,N> {
    read_latency 1
    init coefficients<N=N>()
}
```

The complete transitive function/intrinsic dependency identity participates in
the ROM and companion hashes. Compile-time functions still have no filesystem,
network, process, clock, state, random, or environment access; only the compiler
publishes a validated image. The initializer must resolve to exactly the
declared vector shape and element type.

## Typed bit and collection composition

Bit slicing and composition are expressions rather than backend spellings:

```zlang
high   : bits<4> = word[7:4]
flag   : bit = word[0]
joined : bits<8> = concat(high, word[3:0])
same   : u8 = bitcast<u8>(joined)
flat   : vec<8,u8> = reshape(matrix)
```

Slices use inclusive compile-time `[MSB:LSB]` bounds. A single packed-bit
selection `raw[index]` returns `bit`, uses conventional LSB-zero numbering,
and currently requires a compile-time-proven index. This differs intentionally
from vector sequence indexing: `vec` element zero occupies the most-significant
packed region. Scalar `concat` places its
first operand at the MSB; homogeneous-vector `concat` preserves collection
order and returns a vector. `reshape` changes only nested vector shape.
`bitcast<T>` uses the canonical layout described in
[Types and numerics](types-and-numerics.md#slicing-concatenation-and-representation):
struct declaration field zero, vector element zero, and tuple `item0` are at the
MSB. It requires exact packed width and performs no resize or scale change. A
source or target containing a nominal enum is rejected; enum representation is
not a public bitcast contract. `pack` and `unpack<T>` remain low-level
compatibility spellings.

For representation-bit checks, `reduce(^, scalar)` and `parity(scalar)` operate
on the stored bits of `bit`, raw bits, and signed/unsigned integers. Reducing a
general vector retains the existing element-wise reduction semantics.

## Generic structs, functions, and operators

Generic parameters have explicit kinds:

```zlang
struct Pair<type T> { left:T right:T }

fn duplicate<type T>(x:T) { x + x }

module Use {
    in x : u8
    out y : u9
    y = duplicate(x)
}
```

Calls support exact call-site inference or explicit specialization. Generic
struct constructors infer their specialization from fields where unambiguous.
Every accepted call is monomorphized; there is no runtime dispatch.

A direct integer literal may use a concrete formal argument type after explicit
generic arguments or non-literal arguments have fixed that type:

```zlang
same<T=u16>(1)
combine(1, existing_u16)
combine(existing_u16, 1)
```

If a type is inferable only from literals, each literal first receives its own
minimum-width exact type and normal exact unification applies. Argument order
does not select a wider type. Compound expressions never receive this
literal-only contextual treatment.

Libraries may overload binary `+`, `-`, `*` and unary `-` for nominal structs.
Built-in scalar/fixed operations cannot be replaced. Resolution uses exact type
unification, prefers an exact concrete overload over a generic one, performs no
implicit conversion, and enforces nominal-owner coherence. Specialized bodies
are retained once as deterministic monomorphic callable definitions. Each use
site is a typed `Call` carrying the exact callee identity, signature, result
type, and its own source origin. Analyses that require concrete arithmetic use
the shared bounded call-expansion service; nominal reductions remain opaque to
M32 and the frozen e-graph rewrite set. Direct SystemVerilog emits one
`function automatic` per specialization rather than cloning the body at every
call site.

`std.math.complex` is ordinary ZLang source defining `Complex<T>`, arithmetic
operators, `Butterfly<S,D>`, and explicit component quantization. For example:

```text
Complex<fixed<18,16>> * Complex<fixed<16,14>>
    -> Complex<fixed<35,30>>
```

The scale-30 result cannot be added to a scale-16 value until explicitly
quantized. Its `complex_sum(values)` helper is a discoverability alias that
returns the same typed `Reduce` as `sum(values)`; it adds no compiler behavior.
Generics and nominal reductions do not imply rescaling, rounding, saturation,
or casts.

Traits, runtime polymorphism, generic methods, function-name overloading,
higher-order functions, recursion extensions, and implicit conversions remain
outside the current slice.
