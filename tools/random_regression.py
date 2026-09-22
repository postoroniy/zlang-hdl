# SPDX-License-Identifier: Apache-2.0
"""Bounded, reproducible GitHub-hosted ZLang source regression.

The parent isolates every generated case in a timed child process.  A single
case can be reproduced without recreating the original shard or calendar day.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile


SUITES = ("expressions", "diagnostics")
COMPILER_TIMEOUT = 30
TOOL_TIMEOUT = 30
CASE_TIMEOUT = 90


def derived_seed(master_seed: str, suite: str, index: int) -> int:
    # A case's identity is independent of shard count, so a failed case can be
    # redistributed or replayed after changing CI parallelism.
    payload = json.dumps([master_seed, suite, index], separators=(",", ":")).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def generate(suite: str, seed: int) -> tuple[str, str, dict[str, int] | None]:
    rng = random.Random(seed)
    if suite == "expressions":
        constant = rng.randrange(256)
        kind = rng.randrange(4)
        if kind == 0:
            expression = f"x ^ {constant}"
            oracle = {str(x): x ^ constant for x in (0, 1, 127, 255)}
        elif kind == 1:
            expression = f"x & {constant}"
            oracle = {str(x): x & constant for x in (0, 1, 127, 255)}
        elif kind == 2:
            expression = f"truncate<8>(x + {constant})"
            oracle = {str(x): (x + constant) & 255 for x in (0, 1, 127, 255)}
        else:
            expression = f"x | {constant}"
            oracle = {str(x): x | constant for x in (0, 1, 127, 255)}
        return (
            f"module Random {{\n    in x:u8\n    out y:u8 = {expression}\n}}\n",
            "valid", oracle,
        )
    if suite == "diagnostics":
        bad = (
            "in x:uint<0> out y:u8 = 0",
            "in x:u16 out y:u8 = x",
            "in x:MissingType out y:u8 = 0",
            "in x:u8 out y:u8 = extend<4>(x)",
        )[rng.randrange(4)]
        return f"module Random {{\n    {bad}\n}}\n", "invalid", None
    raise ValueError(f"unknown suite: {suite}")


def run_command(
    argv: list[str], cwd: Path, log: Path, timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        log.write_text(f"timeout after {timeout}s: {argv[0]}\n{error}\n", encoding="utf-8")
        raise RuntimeError(f"{Path(argv[0]).name} timeout") from error
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    return result


def run_single(suite: str, seed: int, directory: Path) -> tuple[str, str]:
    source, expected, oracle = generate(suite, seed)
    directory.mkdir(parents=True, exist_ok=True)
    design = directory / "testcase.zhl"
    design.write_text(source, encoding="utf-8")
    compiler = shutil.which("zlang")
    if compiler is None:
        return "infrastructure", "zlang executable unavailable"
    rtl = directory / "generated.sv"
    command = [compiler, str(design), "--top", "Random"]
    command += ["--systemverilog", str(rtl)] if expected == "valid" else ["--check"]
    try:
        result = run_command(command, directory, directory / "compiler.log", COMPILER_TIMEOUT)
        if expected == "invalid":
            output = (result.stdout + result.stderr).lower()
            if result.returncode == 0:
                return "invalid-accepted", "invalid program was accepted"
            if "traceback" in output or "internal error" in output:
                return "compiler-crash", "invalid program caused an internal error"
            if not output.strip():
                return "diagnostic-missing", "compiler rejected without a diagnostic"
            return "passed", "controlled rejection"
        if result.returncode:
            output = (result.stdout + result.stderr).lower()
            kind = "compiler-crash" if "traceback" in output else "compiler-rejection"
            return kind, f"compiler exited {result.returncode}"
        if not rtl.is_file():
            return "rtl-missing", "compiler succeeded without an RTL artifact"
        verilator = shutil.which("verilator")
        if verilator is None:
            return "infrastructure", "Verilator executable unavailable"
        lint = run_command(
            [verilator, "--lint-only", "-Wno-DECLFILENAME", "-Wno-UNUSED",
             "-Wno-UNDRIVEN", "--top-module", "Random", str(rtl)],
            directory, directory / "verilator.log", TOOL_TIMEOUT,
        )
        if lint.returncode:
            return "rtl-lint", f"Verilator exited {lint.returncode}"
        if oracle is None:
            return "passed", "compiler and RTL lint"
        iverilog = shutil.which("iverilog")
        vvp = shutil.which("vvp")
        if iverilog is None or vvp is None:
            return "infrastructure", "Icarus/VVP executable unavailable"
        lines = ["module tb;", "reg [7:0] x;", "wire [7:0] y;", "Random dut(.x(x), .y(y));", "initial begin"]
        for value in oracle:
            lines.append(f"x = 8'd{value}; #1; $display(\"%0d:%0d\", x, y);")
        lines.extend(["$finish;", "end", "endmodule"])
        bench = directory / "tb.sv"
        bench.write_text("\n".join(lines) + "\n", encoding="utf-8")
        binary = directory / "tb.out"
        compiled = run_command(
            [iverilog, "-g2012", "-s", "tb", "-o", str(binary), str(rtl), str(bench)],
            directory, directory / "iverilog.log", TOOL_TIMEOUT,
        )
        if compiled.returncode:
            return "rtl-compile", f"Icarus exited {compiled.returncode}"
        executed = run_command([vvp, str(binary)], directory, directory / "simulator.log", TOOL_TIMEOUT)
        if executed.returncode:
            return "simulation", f"VVP exited {executed.returncode}"
        actual = dict(line.split(":", 1) for line in executed.stdout.splitlines() if ":" in line)
        (directory / "expected.json").write_text(json.dumps(oracle, sort_keys=True) + "\n")
        (directory / "actual.json").write_text(json.dumps(actual, sort_keys=True) + "\n")
        if actual != {key: str(value) for key, value in oracle.items()}:
            return "output-mismatch", "RTL simulation differs from independent arithmetic oracle"
        return "passed", "compiler, lint and RTL behavior"
    except RuntimeError as error:
        return "timeout", str(error)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES, required=True)
    parser.add_argument("--master-seed", default=datetime.now(timezone.utc).strftime("%Y%m%d"))
    parser.add_argument("--tests", type=int, default=64)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--save-failures", type=Path, default=Path("build/random-failures"))
    parser.add_argument("--single", action="store_true")
    parser.add_argument("--seed", type=lambda value: int(value, 0))
    parser.add_argument("--case-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.tests < 1 or args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error("tests/shards must be positive and shard must be in range")
    if args.single:
        if args.seed is None or args.case_dir is None:
            parser.error("--single requires --seed and --case-dir")
        status, explanation = run_single(args.suite, args.seed, args.case_dir)
        print(json.dumps({"status": status, "explanation": explanation}, sort_keys=True))
        return 0 if status == "passed" else 1

    failures = 0
    total = 0
    timeouts = 0
    for index in range(args.tests):
        if index % args.shards != args.shard:
            continue
        total += 1
        seed = derived_seed(args.master_seed, args.suite, index)
        with tempfile.TemporaryDirectory(prefix="zlang-random-") as scratch:
            case_dir = Path(scratch)
            command = [sys.executable, str(Path(__file__).resolve()), "--suite", args.suite,
                       "--single", "--seed", hex(seed), "--case-dir", str(case_dir)]
            try:
                worker = subprocess.run(command, capture_output=True, text=True,
                                        timeout=CASE_TIMEOUT, check=False)
                response = json.loads(worker.stdout.splitlines()[-1])
                status = response["status"]
                explanation = response["explanation"]
            except (subprocess.TimeoutExpired, IndexError, ValueError, KeyError) as error:
                status = "worker-timeout" if isinstance(error, subprocess.TimeoutExpired) else "worker-crash"
                explanation = str(error)
                worker = None
            if status == "passed" and worker is not None and worker.returncode == 0:
                continue
            failures += 1
            timeouts += int("timeout" in status)
            target = args.save_failures / f"{args.suite}-{index:06d}-{seed:016x}"
            target.mkdir(parents=True, exist_ok=True)
            for source in case_dir.iterdir():
                if source.is_file() and source.name != "tb.out":
                    shutil.copy2(source, target / source.name)
            if not (target / "testcase.zhl").exists():
                (target / "testcase.zhl").write_text(generate(args.suite, seed)[0], encoding="utf-8")
            reproduction = f"python tools/random_regression.py --suite {args.suite} --seed {hex(seed)} --single --case-dir build/reproduce"
            metadata = {"suite": args.suite, "master_seed": args.master_seed,
                        "derived_seed": hex(seed), "shard": args.shard,
                        "shards": args.shards, "test_index": index,
                        "git_commit": os.environ.get("GITHUB_SHA", "unknown"),
                        "failure_kind": status, "explanation": explanation,
                        "reproduce": reproduction}
            (target / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
            print(f"FAIL {status}: {explanation}\nReproduce: {reproduction}", flush=True)
    summary = {"suite": args.suite, "master_seed": args.master_seed,
               "shard": args.shard, "shards": args.shards,
               "generated": total, "passed": total - failures,
               "failed": failures, "timeouts": timeouts}
    print("ZLang Random Regression " + json.dumps(summary, sort_keys=True))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as report:
            report.write(f"\n### Random regression: {args.suite} shard {args.shard + 1}/{args.shards}\n\n")
            report.write(f"Seed `{args.master_seed}`; {total} cases; {failures} failures; {timeouts} timeouts.\n")
    return int(failures != 0)


if __name__ == "__main__":
    raise SystemExit(main())
