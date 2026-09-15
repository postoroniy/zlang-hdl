# E-graph and physical optimization architecture

Status: implemented.

The compiler keeps four distinct responsibilities:

```text
canonical typed value IR
  -> exact typed egglog alternatives
  -> deterministic scalar DAG scheduling
  -> target resource matching
  -> ScheduledValueGraph -> M39/M36/direct SystemVerilog
```

Egglog changes only pure zero-latency values. It does not insert registers or
name FPGA primitives. The scheduler assigns exact cycles and alignment delays,
but does not invent algebraic equalities. Resource matchers bind already typed
operations or subgraphs to source-described resources. `ScheduledValueGraph`
is the authoritative physical bridge consumed by verification and RTL emission.

The production rewrite path has one engine: pinned `egglog==13.2.0`. The old
recursive Python saturation engine has been removed. Stable terms/results and
rendering live in `opt/rewrite_model.py`; rule identity/provenance lives in
`opt/rewrite_spec.py`; guard evaluation lives in `opt/rewrite_guards.py`; the
canonical adapter remains in `opt/egraph.py`; egglog execution and bounded
extraction remain in `opt/saturation.py`.

The optimizer is fail-closed. An operation needs an explicit capability for
e-graph admission or scalar scheduling. Width, truncation, bit reinterpretation,
fixed rescale, rounding, saturation, signedness changes, state and effect nodes
are semantic barriers unless one specifically registered exact rule proves the
requested transformation. Target resource interest is annotation only and does
not change value identity.

Current resource matching supports the accepted Xilinx 7-Series DSP48E1 subset.
Future resource families implement the same matcher contract; they do not add
vendor tests to the general scheduler or algebraic rules to egglog.

Target-independent architecture-interest labels describe shapes such as
multiply-add and preadder-multiply. They neither choose a primitive nor affect
semantic identity. `OperationCostModel` estimates an already chosen operation
implementation; `ResourceMatcher` decides whether a typed operation fits one
source-described resource. Missing timing evidence never becomes zero delay.

Current intentional separations are retained:

- canonical `ExpressionOp` capability and typed-expression class dispatch are
  connected by one closed mapping, but the two IR layers are not merged;
- formal observability remains binding- and ownership-dependent, so it is not
  inferred from pure-value capability alone;
- target planner subgraph covering remains separate from scalar operation
  matching because DSP cascade legality is a graph property, not a node cost.
