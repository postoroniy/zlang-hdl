# Clash versus direct SystemVerilog measurements

Schema: `zlang-backend-comparison-v1`. Timing repetitions: 1.

| Category | Design | ZLang LOC | Clash Haskell LOC | Clash Verilog LOC | Direct SV LOC | Clash LUT/FF/depth | Direct LUT/FF/depth | Clash codegen ms | Direct emit ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| datapath | ALU | 13 | 12 | 21 | 10 | 135/0/14 | 135/0/14 | 1469.466 | 0.041 |
| pipeline | PipelinedMAC | 11 | 20 | 33 | 23 | 136/34/8 | 136/34/8 | 2145.272 | 0.058 |
| ready_valid | RvPassthrough | 7 | 26 | 23 | 14 | 0/0/0 | 0/0/0 | 2282.481 | 0.032 |
| credit | CreditSource | 9 | 40 | 53 | 26 | 4/2/1 | 4/2/1 | 2357.006 | 0.025 |
| csr | ControlCsr | 18 | 39 | 74 | 55 | 17/5/3 | 17/5/3 | 2312.063 | 0.086 |
| request_response | RequestClient | 25 | 71 | 253 | 78 | 23/8/3 | 17/8/3 | 2861.967 | 0.053 |
| rules | RuleCounter | 16 | 23 | 32 | 21 | 11/8/2 | 10/8/2 | 2162.096 | 0.042 |

All cases passed the same behavioral testbench through both RTL paths.
Historical M24 decision: `retain_clash_default_keep_direct_systemverilog_experimental; revisit_region_partition_after_source_mapping_and_broader_backend_coverage`.
That decision predates the completed direct-SV expansion. The current policy is
Clash primary/reference and direct SystemVerilog supported-secondary for the
fail-closed subset documented in `docs/direct-systemverilog.md`.

Tool versions:

- Clash: `Clash, version 1.11.0 (using clash-lib, version: 1.11.0)`
- Verilator: `Verilator 5.044 2026-01-01 rev v5.044`
- Yosys: `Yosys 0.64 (git sha1 6d2c445ae, g++ 13.3.0-6ubuntu2~24.04.1 -fPIC -O3)`
- Icarus Verilog: `Icarus Verilog version 13.0 (devel) (s20251012-54-g6651df6f2)`
