# SPDX-License-Identifier: Apache-2.0
"""Proof-only word-level normalization of actual RTL before temporal proof.

Only independently equivalent combinational cones are replaced. No data,
reset, state or property assumption is introduced. Yosys and Z3 check the
emitted helper round-trip before the original state/control miter executes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

import z3
from zlang.common.subprocess import run_bounded_process

from zlang.formal_bitvector_rewrite import (
    ArithmeticProofUnavailable,
    CheckedBitvectorRewriter,
)
from zlang.formal_rtl_cones import RtlArithmeticCones
from zlang.formal_joint_value import RTL_ARITHMETIC_PROOF_SCHEMA


def _digest(text):
    return sha256(text.encode()).hexdigest()


def _expression(value, args):
    kind, width = value.decl().kind(), value.size()
    if z3.is_bv_value(value):
        return f"{width}'h{value.as_long():x}"
    if kind == z3.Z3_OP_UNINTERPRETED and value.num_args() == 0:
        return str(value.decl().name())
    if kind in (z3.Z3_OP_BADD, z3.Z3_OP_BMUL, z3.Z3_OP_BSUB):
        operation = {z3.Z3_OP_BADD: "+", z3.Z3_OP_BMUL: "*", z3.Z3_OP_BSUB: "-"}[kind]
        return f"{width}'(" + f" {operation} ".join(f"({arg})" for arg in args) + ")"
    if kind == z3.Z3_OP_BNEG:
        return f"{width}'(-({args[0]}))"
    if kind in (z3.Z3_OP_SIGN_EXT, z3.Z3_OP_ZERO_EXT):
        cast = "$signed" if kind == z3.Z3_OP_SIGN_EXT else "$unsigned"
        return f"{width}'({cast}({args[0]}))"
    if kind == z3.Z3_OP_CONCAT:
        return "{" + ", ".join(args) + "}"
    if kind == z3.Z3_OP_EXTRACT:
        high, low = value.params()
        return f"{args[0]}[{high}]" if high == low else f"{args[0]}[{high}:{low}]"
    raise ArithmeticProofUnavailable("unsupported normalized RTL arithmetic operator")


def _helper(name, expression, leaves):
    ports = [f"input wire [{len(leaf.bits) - 1}:0] {leaf.name}" for leaf in leaves]
    ports.append(f"output wire [{expression.size() - 1}:0] result")
    names, lines = {}, []

    def visit(value):
        if value.get_id() in names:
            return names[value.get_id()]
        if len(names) >= 4096:
            raise ArithmeticProofUnavailable("RTL helper node bound exceeded")
        children = tuple(visit(child) for child in value.children())
        if value.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            result = _expression(value, children)
        else:
            result = f"n{len(names)}"
            lines.append(f"wire [{value.size() - 1}:0] {result};")
            lines.append(f"assign {result} = {_expression(value, children)};")
        names[value.get_id()] = result
        return result

    result = visit(expression)
    return (
        f"module {name}("
        + ", ".join(ports)
        + ");\n"
        + "\n".join(lines)
        + f"\nassign result = {result};\nendmodule\n"
    )


def _bounded_degree(expression):
    memo = {}

    def visit(value):
        if value.get_id() in memo:
            return memo[value.get_id()]
        children = [visit(child) for child in value.children()]
        degree = (
            sum(children)
            if value.decl().kind() == z3.Z3_OP_BMUL
            else max(children, default=0 if z3.is_bv_value(value) else 1)
        )
        if degree > 2:
            raise ArithmeticProofUnavailable(
                "RTL arithmetic degree exceeds bilinear proof bound"
            )
        memo[value.get_id()] = degree
        return degree

    visit(expression)


def _yosys(script, root, deadline, name):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArithmeticProofUnavailable("RTL arithmetic preparation timeout")
    try:
        result = run_bounded_process(
            ("yosys", "-Q", "-T", "-p", script),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as error:
        raise ArithmeticProofUnavailable("RTL arithmetic Yosys timeout") from error
    (root / f"{name}.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise ArithmeticProofUnavailable(
            f"RTL arithmetic Yosys failed: {result.stderr[-1000:]}"
        )


@dataclass(frozen=True)
class RtlArithmeticPreparation:
    source: str
    proof_hash: str
    roots: int
    lemmas: int


def normalize_rtl_arithmetic(
    source: str, *, top: str, work_directory: Path, deadline: float
):
    """Return an exactly equivalent RTL miter plus retained solver evidence."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top) is None:
        raise ArithmeticProofUnavailable("invalid RTL arithmetic top identifier")
    root = work_directory / "rtl_arithmetic"
    root.mkdir(parents=True, exist_ok=True)
    (root / "original.v").write_text(source)
    _yosys(
        f"read_verilog -sv -formal original.v; prep -top {top} -flatten; "
        # Remove only unreachable declarations, not connected opaque cells.
        f"hierarchy -check -top {top} -purge_lib; "
        "check -assert; write_json -noscopeinfo original.json",
        root,
        deadline,
        "original",
    )
    design = json.loads((root / "original.json").read_text())
    if set(design["modules"]) != {top}:
        raise ArithmeticProofUnavailable("RTL arithmetic requires one flattened module")
    module = design["modules"][top]
    cones = RtlArithmeticCones(module)
    rewriter = CheckedBitvectorRewriter(deadline=deadline)
    replacements, helpers = [], []
    roots = cones.roots()
    if not roots or len(roots) > 32:
        raise ArithmeticProofUnavailable("RTL arithmetic root bound exceeded")

    def untouched_hash():
        return _digest(
            json.dumps(
                {
                    **module,
                    "cells": {
                        name: cell
                        for name, cell in module["cells"].items()
                        if name not in roots
                    },
                },
                sort_keys=True,
            )
        )

    untouched = untouched_hash()
    for index, name in enumerate(roots):
        original = cones.root_expression(name)
        _bounded_degree(original)
        normalized = rewriter.normalize(original)
        helper_name = f"zlang_formal_arithmetic_{index}"
        if helper_name in design["modules"]:
            raise ArithmeticProofUnavailable("RTL arithmetic helper name collision")
        leaves = cones.leaves
        helpers.append(_helper(helper_name, normalized, leaves))
        replacements.append((name, helper_name, normalized, leaves, original))
    (root / "helpers.v").write_text("\n".join(helpers))
    _yosys(
        "read_verilog -sv helpers.v; proc; opt_clean; check -assert; "
        "write_json -noscopeinfo helpers.json",
        root,
        deadline,
        "helpers",
    )
    parsed = json.loads((root / "helpers.json").read_text())["modules"]
    records = []
    for index, (name, helper_name, normalized, leaves, original) in enumerate(
        replacements
    ):
        helper = parsed[helper_name]
        reader = RtlArithmeticCones(helper, context=cones.context)
        round_trip = reader.vector(tuple(helper["ports"]["result"]["bits"]))
        bindings = {
            tuple(helper["ports"][leaf.name]["bits"]): leaf.expression
            for leaf in leaves
        }
        try:
            round_trip = z3.substitute(
                round_trip,
                *((leaf.expression, bindings[leaf.bits]) for leaf in reader.leaves),
            )
        except KeyError as error:
            raise ArithmeticProofUnavailable(
                "RTL helper introduced an unbound input"
            ) from error
        solver = z3.SolverFor("QF_BV", ctx=normalized.ctx)
        solver.set(timeout=rewriter.remaining_ms())
        solver.add(z3.simplify(round_trip != normalized, som=True))
        query = solver.sexpr() + "\n(check-sat)\n"
        if solver.check() != z3.unsat:
            raise ArithmeticProofUnavailable(
                "RTL arithmetic helper round-trip is not proven"
            )
        (root / f"helper-{index}.smt2").write_text(query)
        cell = module["cells"][name]
        result_bits = cell["connections"]["Y"]
        cell.update(
            type=helper_name,
            parameters={},
            port_directions={
                **{leaf.name: "input" for leaf in leaves},
                "result": "output",
            },
            connections={
                **{leaf.name: list(leaf.bits) for leaf in leaves},
                "result": result_bits,
            },
        )
        records.append(
            {
                "cell": name,
                "source": original.sexpr(),
                "normalized": normalized.sexpr(),
                "bindings": [{"name": leaf.name, "bits": leaf.bits} for leaf in leaves],
                "round_trip_query_hash": _digest(query),
                "round_trip_result": "unsat",
            }
        )
    for index, lemma in enumerate(rewriter.lemmas):
        (root / f"widening-{index}.smt2").write_text(lemma.query)
    if untouched_hash() != untouched:
        raise ArithmeticProofUnavailable(
            "RTL arithmetic changed state, bindings or formal controls"
        )
    (root / "normalized.json").write_text(json.dumps(design, sort_keys=True))
    _yosys(
        f"read_json normalized.json; read_verilog -sv helpers.v; "
        f"hierarchy -check -top {top}; flatten; opt_clean; check -assert; "
        # Stable arithmetic metadata; state/property names are untouched.
        "autoname t:$add t:$sub t:$mul t:$neg t:$pos t:$eq t:$ne; "
        "write_verilog normalized.v",
        root,
        deadline,
        "normalized",
    )
    normalized_source = (root / "normalized.v").read_text()
    proof = {
        "schema": RTL_ARITHMETIC_PROOF_SCHEMA,
        "source_hash": _digest(source),
        "original_netlist_hash": _digest((root / "original.json").read_text()),
        "normalized_netlist_hash": _digest((root / "normalized.json").read_text()),
        "helper_source_hash": _digest((root / "helpers.v").read_text()),
        "helper_netlist_hash": _digest((root / "helpers.json").read_text()),
        "unchanged_state_and_controls_hash": untouched,
        "normalized_source_hash": _digest(normalized_source),
        "solver": z3.get_version_string(),
        "assumptions_added": 0,
        "roots": records,
        "widening_lemmas": [
            asdict(lemma) | {"result": "unsat"} for lemma in rewriter.lemmas
        ],
    }
    text = json.dumps(proof, sort_keys=True, indent=2) + "\n"
    (root / "proof.json").write_text(text)
    return RtlArithmeticPreparation(
        normalized_source, _digest(text), len(records), len(rewriter.lemmas)
    )


def run_rtl_arithmetic_formal(source, **options):
    """Prove the original miter through checked cone substitution and SBY.

    The original formal timeout is shared by preparation and temporal proof.
    Failure to establish a local lemma never turns into an assumed equality.
    """
    from zlang.formal import run_verilog_formal, resolve_formal_toolchain
    from zlang.ir.formal import FormalResult, FormalStatus

    options = dict(options)
    context = resolve_formal_toolchain(
        engine=options.get("engine", "sby"),
        solver=options["solver"],
        route=options.get("route", "smtbmc"),
        toolchain=options.get("toolchain"),
    )
    options["toolchain"] = context
    if context.missing:
        return run_verilog_formal(source, **options)
    timeout = options["timeout_seconds"]
    deadline = time.monotonic() + timeout
    temporary = None
    if options.get("work_directory") is None:
        temporary = tempfile.TemporaryDirectory(prefix="rtl-arithmetic-")
        options["work_directory"] = Path(temporary.name)
    root = Path(options["work_directory"])
    try:
        try:
            prepared = normalize_rtl_arithmetic(
                source, top=options["top"], work_directory=root, deadline=deadline
            )
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                raise ArithmeticProofUnavailable(
                    "RTL arithmetic preparation exhausted formal timeout"
                )
        except (ArithmeticProofUnavailable, z3.Z3Exception) as error:
            return FormalResult(
                options["property_id"],
                FormalStatus.UNKNOWN,
                options["mode"],
                "sby",
                options["solver"],
                options["depth"],
                source_origin=options.get("source_origin"),
                tool_versions=context.versions,
                reason=f"checked RTL arithmetic preparation unavailable: {error}",
            )
        options["timeout_seconds"] = remaining
        result = run_verilog_formal(prepared.source, **options)
        reason = (
            f"solver-checked RTL arithmetic ({prepared.roots} cones, "
            f"{prepared.lemmas} widening lemmas, no added assumptions); "
            f"proof_sha256={prepared.proof_hash}"
        )
        return replace(
            result, reason=reason + (f"; {result.reason}" if result.reason else "")
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
