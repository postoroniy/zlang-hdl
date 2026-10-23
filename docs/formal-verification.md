<a id="reference-formal-verification"></a>
# Formal verification

ZLang HDL supports named same-cycle safety properties and bounded cover goals.
Place goals in the source next to the hardware they describe:

```zlang
module CheckedCounter {
    clock clk reset rst
    in enable : bit
    out value : u8

    reg count : u8 = 0
    step: when enable { count <- truncate<8>(count + 1) }
    value = count

    assert never_wraps_early @ clk { (count < 255) | !enable }
    cover reaches_ten @ clk { count == 10 }
}
```

Run all applicable goals for a selected top:

```sh
zlang design.zhl --top CheckedCounter --verify \
  --formal-jobs 4 \
  --verification-report build/verification.txt \
  --verification-work-dir build/verification-work
```

For a reproducible two-step flow, first publish a verification bundle and then
run it:

```sh
zlang design.zhl --top CheckedCounter \
  --verification-bundle build/verify
zlang verify build/verify --mode bmc --depth 20 \
  --work-dir build/verify-work
```

The work directory contains solver logs and any counterexample or cover VCD.
Keep it outside the bundle so reruns do not modify the input being checked.

Result terms have precise meanings:

- `bounded_pass`: no counterexample was found through the stated depth; this is
  not an unbounded proof;
- `proven`: the selected proof flow completed an unbounded proof;
- `witnessed`: a cover goal was reached and has a witness;
- `bounded_unreached`: a cover was not reached within the selected bound;
- `unknown`, `unavailable`, or `skipped`: no proof claim was established.

Each clocked goal names its clock with `@ clock_name`. Reset behavior follows
the module's declared polarity, assertion mode, and release contract. Goals in
independent supported domains run separately; this does not create a
cross-domain temporal property. Use explicit assumptions only for real
environment constraints, and never use an assumption to hide an invalid design.

Formal execution requires Yosys, SymbiYosys, `yosys-smtbmc`, and a supported
solver such as Z3. The installation chapter shows how to check those tools.
Examples are available in
[`examples/verification`](../examples/verification/README.md).
