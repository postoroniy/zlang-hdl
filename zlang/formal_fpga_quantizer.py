# SPDX-License-Identifier: Apache-2.0
"""Unconditional source-bound final-conversion lemmas, never free cutpoints.

Only the compiler's marked 16/14 FIR conversion is recognized. The actual
connected combinational cone is checked against the independent semantic
reference for every raw input before its stateless implementation is replaced.
All work shares the caller's existing deadline and QF_BV bounds.
"""

from __future__ import annotations
from hashlib import sha256
import json
import time
import z3
from zlang.equivalence import emit_reference_model
from zlang.formal_bitvector_rewrite import ArithmeticProofUnavailable
from zlang.formal_rtl_cones import RtlArithmeticCones


def checked_fpga_quantizers(design, top, root, deadline, yosys):
    module = design["modules"][top]
    records = []
    marked = [
        (name, net)
        for name, net in sorted(module["netnames"].items())
        if "zlang_fir_quantizer" in net.get("attributes", {})
    ]
    if len(marked) > 1:
        raise ArithmeticProofUnavailable("FPGA quantizer count bound exceeded")
    for index, (name, net) in enumerate(marked):
        attrs = net["attributes"]
        try:
            width, fraction, out_width, out_fraction = map(
                int, attrs["zlang_fir_quantizer"].split(":")
            )
            scope = name.rpartition(".")[0]
            raw_name = (scope + "." if scope else "") + attrs[
                "zlang_fir_quantizer_input"
            ]
            raw_bits = tuple(module["netnames"][raw_name]["bits"])
        except (ValueError, KeyError, TypeError) as error:
            raise ArithmeticProofUnavailable(
                "invalid FPGA quantizer mapping"
            ) from error
        if not (
            1 <= width <= 54
            and 14 < fraction < width
            and (out_width, out_fraction) == (16, 14)
            and len(raw_bits) == width
            and len(net["bits"]) == out_width
        ):
            raise ArithmeticProofUnavailable("unqualified FPGA quantizer format")
        actual = RtlArithmeticCones(module, logic=True, input_words=(raw_bits,))
        value = actual.vector(tuple(net["bits"]))
        leaves = actual.leaves
        if len(leaves) != 1 or leaves[0].bits != raw_bits:
            raise ArithmeticProofUnavailable(
                "quantizer has unbound state/control/input dependencies"
            )
        # Reuse typed numeric facts; there is no ZLang re-parser in the checker.
        from zlang.ir import expressions as e
        from zlang.ir.types import FixedType

        raw = e.InputRef("raw", FixedType(width, fraction))
        conversion = e.FixedConvert(
            expression=raw,
            type=FixedType(out_width, out_fraction),
            kind=e.FixedConversionKind.RESCALE,
            rounding=e.FixedRounding.NEAREST_EVEN,
            overflow=e.FixedOverflow.SATURATE,
        )
        model_name = f"zlang_formal_fir_quantizer_{index}"
        text = emit_reference_model(
            model_name, "result", conversion.type, (("raw", raw.type),), conversion
        )
        sv = root / f"quantizer-{index}.v"
        sv.write_text(text)
        yosys(
            f"read_verilog -sv {sv.name}; proc; opt_clean; check -assert; write_json -noscopeinfo quantizer-{index}.json",
            root,
            deadline,
            f"quantizer-{index}",
        )
        model = json.loads((root / f"quantizer-{index}.json").read_text())["modules"][
            model_name
        ]
        ref = RtlArithmeticCones(model, logic=True, context=actual.context)
        expected = ref.vector(tuple(model["ports"]["result"]["bits"]))
        if len(ref.leaves) != 1 or ref.leaves[0].bits != tuple(
            model["ports"]["raw"]["bits"]
        ):
            raise ArithmeticProofUnavailable(
                "quantizer model is not a pure one-input function"
            )
        expected = z3.substitute(
            expected, (ref.leaves[0].expression, leaves[0].expression)
        )
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining < 1:
            raise ArithmeticProofUnavailable("quantizer proof deadline exhausted")
        solver = z3.SolverFor("QF_BV", ctx=actual.context)
        solver.set(timeout=min(3000, remaining))
        solver.add(value != expected)
        query = solver.sexpr() + "\n(check-sat)\n"
        (root / f"quantizer-{index}.smt2").write_text(query)
        if solver.check() != z3.unsat:
            raise ArithmeticProofUnavailable(
                "actual quantizer cone equivalence is not proven"
            )
        producer = actual._drivers.get(net["bits"][0])
        if producer is None or producer[0][0] != "cell":
            raise ArithmeticProofUnavailable("quantizer output has no complete driver")
        cell_name = producer[0][1]
        cell = module["cells"][cell_name]
        if cell["type"] != "$mux" or tuple(cell["connections"]["Y"]) != tuple(
            net["bits"]
        ):
            raise ArithmeticProofUnavailable(
                "quantizer root is not a complete combinational output"
            )
        original = json.dumps(cell, sort_keys=True)
        cell.update(
            type=model_name,
            parameters={},
            port_directions={"raw": "input", "result": "output"},
            connections={"raw": list(raw_bits), "result": list(net["bits"])},
        )
        design["modules"][model_name] = model
        records.append(
            {
                "schema": "zlang-checked-fir-quantizer-v1",
                "cell": cell_name,
                "input_bits": raw_bits,
                "output_bits": net["bits"],
                "format": [width, fraction, out_width, out_fraction],
                "actual_cell_sha256": sha256(original.encode()).hexdigest(),
                "model_source_sha256": sha256(text.encode()).hexdigest(),
                "query_sha256": sha256(query.encode()).hexdigest(),
                "result": "unsat",
                "assumptions_added": 0,
            }
        )
    return records
