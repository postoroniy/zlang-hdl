#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fresh compiler-selected architecture synthesis/route/pack; no programming."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
from zlang.fpga_boards import load_board
from zlang.fpga_build import build_fpga

BOARDS = ("brs-100-gw1nr9-r1.0", "orangecrab-r0.2-25f", "orangecrab-r0.2.1-85f")
ARITHMETIC = (
    ("Multiply", "Multiply"),
    ("UnsignedMultiply", "Multiply"),
    ("MultiplyAdd", "MultiplyAddFabric"),
    ("PreaddMultiply", "MultiplyAddFabric"),
    ("SignedReduction", "SignedProductFabric"),
    ("Reduction4", "SignedProductFabric4"),
    ("SymmetricFirAuto", "SymmetricFirFabric"),
)


def native_register_modes(path, family, top):
    """Validate mapped hard-multiplier modes, separately from site counts."""
    design = json.loads(path.read_text())
    primitive = "MULT18X18" if family == "gowin" else "MULT18X18D"
    native = 0 if top.startswith(("Parallel", "SymmetricFir")) else int(top[-1]) - 1
    if native not in (0, 1, 2):
        raise ValueError("unknown qualified pipeline")
    expected = (
        {"AREG": 0, "BREG": 0, "PIPE_REG": int(native > 0), "OUT_REG": int(native > 1)}
        if family == "gowin"
        else {
            "REG_INPUTA_CLK": "NONE",
            "REG_INPUTB_CLK": "NONE",
            "REG_PIPELINE_CLK": "CLK0" if native else "NONE",
            "REG_OUTPUT_CLK": "CLK0" if native > 1 else "NONE",
            "RESETMODE": "SYNC",
        }
    )
    rows = []
    for module_name, module in sorted(design["modules"].items()):
        for name, cell in sorted(module.get("cells", {}).items()):
            if cell["type"] != primitive:
                continue
            parameters = cell["parameters"]
            actual = {}
            for key, value in expected.items():
                raw = parameters.get(key)
                if type(value) is int:
                    raw = (
                        int(raw, 2)
                        if isinstance(raw, str) and set(raw) <= {"0", "1"}
                        else raw
                    )
                if raw != value:
                    raise ValueError(
                        f"unqualified mapped {primitive} {key}: {raw!r} != {value!r}"
                    )
                actual[key] = raw
            rows.append(
                {
                    "module": module_name,
                    "cell": name,
                    "primitive": primitive,
                    "parameters": actual,
                }
            )
    if not rows:
        raise ValueError("missing mapped hard multipliers")
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--toolchain-manifest", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--board", choices=BOARDS, action="append")
    args = p.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        p.error("fresh output required")
    args.output.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    rows = []
    failed = False
    for board in args.board or BOARDS:
        family = load_board(board).record["family"]
        prefix = "Gowin" if family == "gowin" else "Ecp5"
        cases = [
            (
                "arithmetic",
                top + str(n),
                arch
                + (
                    (("", "Delay2", "Delay3")[n - 1])
                    if top == "SymmetricFirAuto"
                    else suf
                ),
            )
            for top, arch in ARITHMETIC
            for n, suf in ((1, ""), (2, "MREG"), (3, "MREGOREG"))
        ]
        cases += [
            ("arithmetic", f"Parallel{n}", f"ParallelProducts{n}") for n in range(1, 9)
        ]
        cases += [
            ("storage", name, "BRAM")
            for name in (
                "RamReadFirst",
                "RamWriteFirst",
                "RamNoChange",
                "RamTrueDual",
                "RamTrueDualNoChange",
            )
        ]
        cases += [("storage", "AsyncFifoStorage", "AsyncFifoBRAM")]
        for source, top, arch in cases:
            directory = args.output / board / top
            try:
                receipt = build_fpga(
                    root / "examples/fpga/architectures" / f"{source}.zhl",
                    board=board,
                    toolchain_manifest=args.toolchain_manifest,
                    output=directory,
                    top=top,
                    architecture=prefix + arch,
                    architecture_mode="required",
                    self_test_wrapper=True,
                )
                counts = receipt["isolated_core_synthesis_cells"]
                modes = []
                if source == "arithmetic":
                    expected = (
                        8
                        if top == "Parallel8"
                        else (
                            int(top[-1])
                            if top.startswith("Parallel")
                            else 4
                            if top.startswith(("Reduction4", "Symmetric"))
                            else 2
                            if top.startswith("SignedReduction")
                            else 1
                        )
                    )
                    primitive = "MULT18X18" if family == "gowin" else "MULT18X18D"
                    modes = native_register_modes(directory / "core.json", family, top)
                    if counts.get(primitive) != expected:
                        raise ValueError(
                            f"wrong mapped hard multiplier count: {counts}"
                        )
                else:
                    allowed = (
                        {
                            "DPX9",
                            "DPX9B",
                            "SPX9",
                            "SPX9B",
                            "SDPX9",
                            "SDPX9B",
                            "DPB",
                            "SP",
                            "SDPB",
                        }
                        if family == "gowin"
                        else {"DP16KD", "PDPW16KD"}
                    )
                    if sum(v for k, v in counts.items() if k in allowed) != 1:
                        raise ValueError(f"wrong block-memory count: {counts}")
                row = dict(
                    board=board,
                    top=top,
                    architecture=prefix + arch,
                    state="packed",
                    build_identity=receipt["build_identity"],
                    core_cells=counts,
                    mapped_native_register_modes=modes,
                    fixture_cells=receipt["synthesis_cells"],
                    routing=receipt["routing"],
                    hardware="UNKNOWN",
                )
            except Exception as error:
                failed = True
                row = dict(
                    board=board,
                    top=top,
                    architecture=prefix + arch,
                    state="failed",
                    error=f"{type(error).__name__}: {error}",
                )
            rows.append(row)
            (args.output / "summary.json").write_text(
                json.dumps(
                    dict(
                        schema="zlang-fpga-architecture-qualification-v1",
                        results=rows,
                        passed=not failed,
                        hardware="UNKNOWN",
                        vendor_gowin_eda="UNKNOWN",
                    ),
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            print(board, top, row["state"], row.get("error", ""), flush=True)
            if failed:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
