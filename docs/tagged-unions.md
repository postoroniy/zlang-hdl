# Nominal tagged unions

ZLang tagged unions are a bounded, backend-independent way to carry one of
several named scalar payload shapes. They are values, not procedural control
flow and not protocol envelopes.

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

## Type and layout

The type is nominal: two equal-looking declarations are not interchangeable.
Variant order defines ordinal tag codes. The tag width is
`max(1, ceil_log2(variant_count))` and occupies the most-significant bits. The
payload is as wide as the largest variant. Fields occupy it from most to least
significant in declaration order; a shorter payload has zero low-order padding.

The first supported slice admits only flat `bit`, `bits<N>`, `uN`/`uint<N>`,
`sN`/`sint<N>`, `fixed`, and `ufixed` fields. A fieldless value uses the concise
constructor `Message.Idle`; `Message.Idle {}` remains an equivalent explicit
spelling.

## Construction and matching

Constructor fields are named, exact, and complete. Missing, extra, repeated,
or wrongly typed fields are compile-time errors. A `match` must contain every
variant exactly once. A payload arm binds every field in declaration order;
binders cannot rename, omit, repeat, or shadow an existing value.

Matching is pure and zero-latency. Semantic lowering retains a typed
`UnionConstruct`, `UnionTag`, and `UnionField`, then expresses selection with the
ordinary typed `Switch`. The simulator carries an immutable nominal runtime
value. Direct SystemVerilog carries the exact frozen packed bits.
Registers and internal scalar child ports may use union values.

## Deliberate boundaries

An external top-level union input is rejected because arbitrary raw bits could
encode an unused tag. There is no raw decode, `bitcast<Union>`, `unpack<Union>`,
generic/recursive union, nested aggregate payload, union operator overload,
wildcard/nested pattern, guard, partial match, or protocol inference in this
slice. No e-graph rewrite or new formal observation is added.

The runnable source is [`examples/tagged_union.zhl`](../examples/tagged_union.zhl).
The exact representation and current exclusions are documented above and in
the [syntax support matrix](syntax-support-matrix.md).
