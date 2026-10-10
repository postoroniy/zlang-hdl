# Stdlib formal coverage

The nightly matrix uses the exact solver executables supplied by its pinned OSS
CAD Suite. Z3 4.13.4 or newer is required because Z3 4.8.12 stalls on the
minimized AXI4 read-endpoint query. A successful formal RTL
artifact build is not a solver result. The nightly job runs executable
Yosys/SymbiYosys contracts and retains its JUnit, solver traces and the
compiler-parser-owned declaration inventory. It is deliberately not a pull
request gate.

The same job additionally publishes one immutable positive bundle and one
deliberately failing bundle, then replays each through required Z3, Boolector
and Bitwuzla routes plus corroborating Yices and cvc5 routes. Every route must
produce the same property/status vector and the expected overall outcome.
Results remain independent solver records; agreement is not represented as a
new proof status and does not promote bounded evidence to `proven`.

The nightly also qualifies independent engine families against those same
immutable bundles.  ABC PDR supplies an independent unbounded safety route;
Pono and btormc supply independent bounded BTOR routes.  Their records are
qualification evidence only: they are not accepted by the public
`zlang verify --route` option and cannot satisfy required-formal selection.
The Avy/AIGER route is retained as a visible observation because the pinned
Avy build in OSS CAD Suite 2026-09-30 reproducibly crashes on one positive
property; that incomplete observation is never counted as a required gate.
BTOR failures are mapped back through exact compiler bindings using route-owned
rising-clock frames because BTOR witness VCDs do not carry the SMT route's
`smt_step` marker.

Run locally with the repository's test environment:

```sh
mkdir -p build
python -m pytest -q -o junit_family=legacy \
  tests/integration/test_stdlib_axi4_z3.py \
  tests/integration/test_stdlib_stream_z3.py \
  tests/integration/test_ztpu_axi_burst.py::test_root_ready_valid_safety_verification_executes_with_real_sby_z3 \
  --junitxml=build/stdlib-formal.xml
python tools/stdlib_formal_coverage.py --junit build/stdlib-formal.xml
```

Use Z3 4.13.4 or newer for the read-endpoint case. The test explicitly skips
that case on older Z3; the nightly no-skips gate rejects such an environment.

The inventory includes every parsed stdlib module, function and declarative
type/protocol/target item. New executable declarations enter as `unverified`;
they never inherit a proof from their containing file. `static-only` means
that no RTL proof is applicable, **not** that a Z3 check ran. `partial` means
that only the named contract and specialization have a result. The evidence
table is checked against parsed declaration identities. Without a JUnit report
all evidence entries say `evidence-unchecked`. With a report, the command
rejects missing, failed or skipped evidence cases. The nightly job runs those
tests in the same checkout before publishing the inventory.
`blocked` records a reproduced solver obstacle; `unsupported` records a
missing formal contract/route. Neither is a passing result.

Current executable contracts:

| Stdlib entity | Contract and configuration | Solver level |
| --- | --- | --- |
| `axi4_response_is_error` | Four `bits<2>` response codes; a wrong-code mutation fails | `PROVEN` |
| `axi4_address_valid` | Legal address implies `size == 0` for `AW=8,DW=8,IW=1`; a constant-legal mutation fails | `BOUNDED_PASS(4)` for this property only |
| `AXI4ReadSubordinate` | Held R payload/valid under backpressure at `AW=8,DW=8,IW=1,D=2,BW=1`, with fixed legal request/backend beat; output-bypass mutation fails | `PROVEN` for this harness property only; module `partial` |
| `RvRegisterSlice` | Ready/valid stability with `T=u8`; an output-bypass mutation fails | `PROVEN` for this property only; module `partial` |
| `RvMux2`, `RvDemux2` | Selected payload/valid and selected-path backpressure at `T=u8`; inverted-select mutations fail | `BOUNDED_PASS(3)` for these routing properties only |
| `RvSkidBuffer`, `RvFifo` | Ready/valid stability at `T=u8,D=2` and `T=FrameBeat<u8,u2>,D=4`; output-bypass mutations fail | `BOUNDED_PASS(6)` only |
| `axi_burst` ZTPU reader/writer compositions | Generated ready/valid safety, depth 6 | `BOUNDED_PASS`, not complete AXI4 compliance |

The ordinary AXI4 read/write managers, remaining subordinate contracts, USER
variants, pin adapters and exclusive path do **not** yet have complete endpoint
Z3 proofs. Their compilation, simulation and formal-artifact tests must not be
reported as proof. The same generated read-endpoint SMT2 query did not finish
step 0 on Z3 4.8.12 within 5 seconds, while Z3 4.13.4 completed depth 2 in
under one second; the source-authored read-endpoint harness then passed an
inductive proof at depth 6 and a failing output-bypass mutation. This is a
solver-version finding, not a ZLang semantic change. The fixed legal request
and backend beat constrain the test, so it does not establish full AXI4
compliance. A reduced `AXI4ReadManager<8,8,1,2>` ready/valid harness timed out
on the older solver; it still needs retesting and a checked-in proof contract.

Full-stdlib verification remains blocked by unverified executable entries
and by formal routes that are currently unsupported, including multi-domain
asynchronous reset goals. For generic entities, any future result applies only
to its recorded finite parameter matrix and assumptions. Use `PROVEN` only
for a successful inductive result; a finite-depth result remains
`BOUNDED_PASS(depth)`. `UNKNOWN`, timeout and unsupported are never success.
