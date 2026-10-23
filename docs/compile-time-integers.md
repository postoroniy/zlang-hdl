# Compile-time integer modulo

`%` is available for integer expressions resolved during elaboration: generic
value arguments, static indices, compile-time `if` conditions, and expressions
whose operands are constants or specialized integer parameters. It has the
same precedence as `*` and `/`, with left-associative product chains.

The [quick reference](language-quick-reference.md#aggregates-and-functions)
summarizes the operator's scope. This example selects a chip from a constant
PSS sequence; each specialization produces a constant `s2`:

```zlang
fn nr_pss_chip<ID,K>() -> s2 {
    mseq = 0x42afa51b8fe1de592044c5d6c19a9cf6
    mseq[(K + 43 * ID) % 127] ? -1 : 1
}

module PssChip {
    out chip : s2 = nr_pss_chip<ID=2,K=126>()
}
```

Modulo uses floor-division semantics: a nonzero result has the sign of the
divisor. For example, `-5 % 3 == 1`, `5 % -3 == -1`, and `-5 % -3 == -2`.
With a positive divisor `D`, the result `r` satisfies `0 <= r < D`, including for a
negative dividend. A zero divisor is an error in an evaluated expression;
unselected compile-time `if` branches are not evaluated.

Generic value arguments can themselves contain `%`, such as
`nr_pss_chip<ID=2,K=(126 + 1) % 127>()`. A condition such as `if K % 2 == 0`
selects a branch during specialization, not a runtime circuit. Static index
results still undergo the normal bounds checks; `%` does not enable unchecked
indexing or change hardware arithmetic widths.

Both operands must be compile-time integers. Input ports, registers and
ordinary function arguments are runtime values, even if a caller supplies a
constant or a register has a reset initializer. Runtime remainder is not a
hardware operator, and this feature does not infer a divider or a fixed-point
remainder. Bounded generated specializations fold modulo with concrete loop
indices; they remain subject to the existing elaboration limits.
