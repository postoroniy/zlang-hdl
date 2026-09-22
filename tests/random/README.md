<!-- SPDX-License-Identifier: Apache-2.0 -->
# Reproducible random regression

The generator produces bounded, grammar-aware `.zhl` cases. The `expressions`
suite checks compilation, Verilator lint and Icarus behavior against an
independent finite-width arithmetic oracle. The `diagnostics` suite checks
controlled rejection of invalid sources. Every case runs in a timed child
process; timeouts, crashes and tool failures are not skipped.

For an optional developer reproduction in a prepared environment:

```bash
python tools/random_regression.py --suite expressions --master-seed 20260923 \
  --tests 128 --shard 0 --shards 4
```

The SHA-256-derived case seed depends on master seed, suite and global test
index, not Python's process hash or shard count. A failure's `metadata.json`
contains the exact `--single --seed` reproduction command. The source, RTL,
testbench, expected/actual values and available logs accompany it in the
GitHub Actions failure artifact. Use that command to reproduce the case,
minimize the source, fix the compiler and add the minimized `.zhl` as a
permanent deterministic regression; do not commit bulk random output.

PR CI uses a fixed seed. `.github/workflows/daily-regression.yml` schedules
the complete deterministic suite plus eight random matrix shards twice daily
on GitHub-hosted Linux runners. A manual dispatch accepts an explicit master
seed; otherwise the UTC date is printed in the run summary. Failures are
uploaded for 14 days. The workstation is not part of the mandatory gate.
