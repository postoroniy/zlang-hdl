# Reviewed generated examples

This directory contains test-owned golden artifacts, not compiler inputs and
not a second source tree. Each file is compared with fresh compiler output by
the owning regression. Update one only after reviewing the exact semantic,
manifest, source-map, or RTL change that caused it.

| Artifact family | Source/owner |
| --- | --- |
| `*.direct.sv` | Direct-SystemVerilog output for the corresponding example module; `tests/systemverilog/test_emitter.py` owns the core set. |
| `ControlCsr.{md,json}` and `EngineCsr.{md,json}` | CSR documentation and schema output; `tests/integration/test_csr.py`. |
| `*.opt` and `ShiftMultiply.saturation` | Canonical optimization/saturation reports; optimization integration tests. |
| `AutoPipelineProducts.pipeline` | Pipeline planning report; `tests/integration/test_pipelines.py`. |
| `FirArchitecture.architecture` | Architecture candidate report; `tests/integration/test_architectures.py`. |
| `CostMac*` reports | Cost, synthesis-feedback, and implementation selection; cost/implementation integration tests. |
| `MacChoice.implementations` | Explicit implementation selection report. |
| `ContractedAdd.contracts.sv` | Source-contract SystemVerilog; verification integration tests. |

Generated reports may contain stable identities. A changed hash is evidence to
investigate, not a reason to refresh every golden mechanically.
