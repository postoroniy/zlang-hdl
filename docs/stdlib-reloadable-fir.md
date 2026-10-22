# Reloadable and folded FIR

These source components use the ordinary single-domain ready/valid contract.
The immutable FIR keeps its existing interface; loading and commit are exposed
only by the separate reloadable variants. Source availability does not imply
that a previously built wheel or extension contains this update.

Import `std.dsp.fir_reloadable`. `RvFirReloadableExact` keeps full signed
products and the balanced dot result. Its output is
`fixed<SW+CW+ceil_log2(N),SF+CF>`. `RvFirReloadableNearestEvenSaturate` adds
`OW,OF` and one final nearest-even/saturating conversion, without another stage.

Both use ready/valid sample input/output and a ready/valid coefficient `reload`
input. Load exactly N coefficients, tap 0 first, into the inactive bank.
Hold `commit` until `commit_ready` is high at a clock edge. A full requested
commit blocks new samples until the old pending output retires. The switch
edge accepts neither a sample nor a coefficient. Loading the last coefficient
and committing require separate edges. Processing may continue while loading.
Every result uses one complete coefficient bank, never a mixture.

Reset restores both banks to `initial_coefficients`, clears sample history,
the pending result and partial reload. Commit preserves sample history.
Unstalled sample latency is one edge, II=1; output backpressure stalls input
acceptance and history. There is one pending result, not an unbounded queue.

Run the example from the repository root after `make venv`:

```bash
.venv/bin/zlang sim examples/fir_reload.zhl --top ReloadableFir \
  --events examples/fir_reload.events.jsonl --json
```

Event payloads are raw fixed-point integers: sample 8 means 1.0 for SF=3;
coefficient 4 means 1.0 for CF=2. The example loads `[1,-1,0.5]`, stalls an
old result, then commits and resumes with the existing sample history.

This is an explicitly parallel library datapath. It does not enable automatic
scheduling, new implementation intents, or coefficient reload in other FIRs.
This contract does not claim universal physical timing or one DSP per tap.

## Explicit one-MAC variants

Import `std.dsp.fir_folded`. `RvFirFoldedExact` takes immutable `coefficients`;
`RvFirFoldedReloadableExact` takes `initial_coefficients` and the same reload/
commit interface described above. Both admit N=1…32. Unstalled latency and II
are N edges: acceptance performs tap 0, then one tap is accumulated per edge.
N=1 is a one-stage FIR with no history array. The full accepted window is
held while busy; samples from different windows never overlap. Input can
retire/refill on the edge that consumes a pending result. Stalls hold that
result; they do not advance sample history. Reload may proceed while busy,
but commit waits for completion and retirement of the old result.

Output width/scaling is the same exact format as the parallel FIR. Optional
`RvFirFoldedNearestEvenSaturate` and
`RvFirFoldedReloadableNearestEvenSaturate` add `OW,OF` and one final conversion,
without another register stage. The serial accumulator never rounds products.
One logical multiplier is not a universal one-DSP guarantee: the qualified
s16×s16 Series-7 instances use one DSP48E1; other widths require measurement.

```bash
.venv/bin/zlang sim examples/fir_folded.zhl --top FoldedFir \
  --events examples/fir_folded.events.jsonl --json
```
