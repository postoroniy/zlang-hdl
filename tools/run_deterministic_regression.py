"""Run the cold Verilator comparison before the parallel deterministic tests."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


# This is the existing parameter that builds Verilator. A renamed case must
# fail collection instead of silently disappearing from the serial partition.
_COLD_CASE = (
    "tests/integration/test_a24_runtime_boundaries.py::"
    "test_local_expression_drives_both_outputs_beside_cdc[d+e-<lambda>-True-True]"
)


def _publish_reports(reports: list[Path], destination: Path) -> None:
    """Keep the original testcases and publish one ordinary pytest JUnit suite."""
    suites = []
    for path in reports:
        raw = ET.parse(path).getroot()
        if raw.tag != "testsuites" or len(raw.findall("testsuite")) != 1:
            raise ValueError(f"expected one raw pytest suite in {path}")
        suites.append(raw.find("testsuite"))

    merged = ET.Element("testsuite", suites[0].attrib.copy())
    keys = set()
    for suite in suites:
        phase_keys = {(case.get("classname"), case.get("name")) for case in suite.findall("testcase")}
        overlap = keys & phase_keys
        if overlap:
            raise ValueError(f"deterministic phases ran the same testcase: {next(iter(overlap))}")
        keys.update(phase_keys)
        merged.extend(suite)
    for field in ("tests", "errors", "failures", "skipped"):
        merged.set(field, str(sum(int(suite.attrib[field]) for suite in suites)))
    merged.set("time", f'{sum(float(suite.attrib["time"]) for suite in suites):.3f}')
    root = ET.Element("testsuites", name="pytest tests")
    root.append(merged)
    staged = reports[0].parent / "combined.xml"
    ET.ElementTree(root).write(staged, encoding="utf-8", xml_declaration=True)
    staged.replace(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--workers", required=True)
    parser.add_argument("--maxfail", type=int, default=0)
    args = parser.parse_args(argv)
    if args.maxfail < 0:
        parser.error("--maxfail must be nonnegative")

    output = args.junit.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    parts = Path(tempfile.mkdtemp(prefix=output.name + ".parts-", dir=output.parent))
    print(f"Raw deterministic reports: {parts}", flush=True)
    # A runner crash must not leave a previous run's combined receipt in place.
    if output.is_file():
        output.replace(parts / "previous.xml")
    common = [
        sys.executable, "-m", "pytest", "-p", "tools.pytest_no_skips", "-q", "-m", "not performance",
    ]
    cold = parts / "cold.xml"
    code = subprocess.run([
        *common, "-n", "0", "--maxfail", str(args.maxfail), "--junitxml", str(cold), _COLD_CASE,
    ]).returncode
    if code not in (0, 1) or not cold.is_file():
        return code or 1
    cases = ET.parse(cold).getroot().findall(".//testcase")
    if code == 0:
        module, name = _COLD_CASE.split("::")
        expected = (module.removesuffix(".py").replace("/", "."), name)
        if [(case.get("classname"), case.get("name")) for case in cases] != [expected]:
            raise ValueError("the cold comparison did not produce its expected testcase")
    failures = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    spent = max(failures, int(code != 0))
    if args.maxfail and spent >= args.maxfail:
        _publish_reports([cold], output)
        return code or 1

    parallel = parts / "parallel.xml"
    remaining = max(0, args.maxfail - spent)
    other = subprocess.run([
        *common, "-n", args.workers, "--dist=loadscope", "--maxfail", str(remaining),
        "--deselect", _COLD_CASE, "--junitxml", str(parallel),
    ]).returncode
    if other not in (0, 1) or not parallel.is_file():
        return other or 1
    _publish_reports([cold, parallel], output)
    return code or other


if __name__ == "__main__":
    raise SystemExit(main())
