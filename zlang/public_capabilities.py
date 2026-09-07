"""Stable public-language capabilities shared by tooling and documentation tests.

This module is deliberately compiler-owned.  Editor metadata and documentation
may describe this surface, but neither is allowed to invent compiler features.
The lexical surface remains separate from capability evidence: semantic and
backend legality still comes from typed analysis and fail-closed emitters.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentationRequirement:
    """A stable marker required in the public support matrix."""

    capability: str
    document: str
    markers: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityWitness:
    """One repository source/top pair that must remain semantically executable."""

    source_path: str
    top: str


@dataclass(frozen=True)
class PublicCapability:
    """Evidence-backed status for one coherent public-language capability.

    The phase fields intentionally use short human-readable status text rather
    than booleans.  A capability may be semantically supported while one
    backend or formal relation remains bounded or unavailable.
    """

    name: str
    context: str
    status: str
    simulator: str
    clash: str
    direct_systemverilog: str
    formal: str
    witness: CapabilityWitness
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicCapabilityRegistry:
    """Versioned stable spellings and their minimum documentation contract."""

    schema_version: int
    keywords: tuple[str, ...]
    types: tuple[str, ...]
    intrinsics: tuple[str, ...]
    modes: tuple[str, ...]
    operators: tuple[str, ...]
    capabilities: tuple[PublicCapability, ...]
    documentation: tuple[DocumentationRequirement, ...]

    def editor_surface(self) -> dict[str, list[str]]:
        """Return the JSON-compatible surface consumed by lexical editors."""

        return {
            "keywords": list(self.keywords),
            "types": list(self.types),
            "intrinsics": list(self.intrinsics),
            "modes": list(self.modes),
            "operators": list(self.operators),
        }

    def capability_matrix(self) -> list[dict[str, object]]:
        """Return deterministic JSON-compatible capability evidence.

        This intentionally is not part of :meth:`editor_surface`; lexical
        editors must not infer semantic or backend legality from highlighting.
        """

        return [
            {
                "name": item.name,
                "context": item.context,
                "status": item.status,
                "simulator": item.simulator,
                "clash": item.clash,
                "direct_systemverilog": item.direct_systemverilog,
                "formal": item.formal,
                "witness": {
                    "source_path": item.witness.source_path,
                    "top": item.witness.top,
                },
                "limitations": list(item.limitations),
            }
            for item in self.capabilities
        ]


CAPABILITY_REGISTRY = PublicCapabilityRegistry(
    schema_version=23,
    keywords=(
        "import", "module", "extern", "model", "struct", "enum", "union", "type", "fn", "operator", "equiv",
        "protocol", "role", "channel", "member", "resource", "target", "device",
        "port", "operation", "class", "capability", "limit", "architecture",
        "register_site", "pipeline_site", "pipeline_config", "location", "local",
        "delay_ps", "enable", "setting", "dedicated", "width", "relation",
        "fallback", "binding", "physical_primitive", "physical_site", "physical_edge",
        "provides", "part", "inventory", "dedicated_capacity", "require_resource",
        "use_pipeline", "require_dedicated",
        "in", "out", "clock", "reset", "async", "edge", "mode", "polarity",
        "power_up", "inst", "connect", "transform", "interface",
        "wire", "rv", "credit", "packet", "vc_credit", "request_response",
        "max_outstanding", "ordering", "match_by", "buffer", "request_buffer",
        "response_buffer", "adapter", "crossing", "async_fifo", "arbiter", "policy",
        "grant", "disable", "iff", "csr", "sticky", "rule", "when", "priority",
        "fsm", "hold",
        "fifo", "memory", "mem", "rom", "read_latency", "init", "collision",
        "contents", "read_data", "reg", "delay",
        "timing",
        "pipeline", "choice", "auto", "explore", "allow", "avoid", "require",
        "minimize", "maximize", "generate", "map", "sum", "reduce", "dot", "quantize",
        "round", "overflow", "with", "if", "else", "switch", "match", "mux",
        "assume", "guarantee", "assert", "cover", "contract", "ensure",
    ),
    types=(
        "bit", "char", "string", "uint", "sint", "bits", "uN", "sN", "fixed", "ufixed",
        "fixed_sat", "ufixed_sat", "SF", "UF", "SF_Sat", "UF_Sat", "vec",
        "fifo", "mem", "rom", "wire", "rv", "credit", "packet", "vc_credit",
        "request_response",
    ),
    intrinsics=(
        "truncate", "extend", "quantize", "pack", "unpack", "bitcast",
        "reshape", "concat", "zeros", "ones", "parity", "enum_encode", "enum_valid",
        "enum_decode",
        "fixed_raw", "fixed_to_raw",
        "fixed_truncate_wrap",
        "fixed_truncate_saturate", "fixed_round_even_wrap",
        "fixed_round_even_saturate", "length", "floor_log2", "ceil_log2", "index_width",
        "is_power_of_two", "pi", "sin", "cos", "log2", "log", "mux",
        "generate", "map", "sum", "reduce", "dot", "repeat", "delay", "pipeline",
        "unsigned", "signed", "width", "same_type", "constant", "power_of_two",
    ),
    modes=(
        "rw", "ro", "wo", "w1c", "pulse", "reserved", "in_order", "out_of_order",
        "fixed_priority", "round_robin", "beat", "packet", "sync_level",
        "pulse_toggle", "handshake", "async_fifo", "rv_to_credit", "credit_to_rv",
        "read_first", "write_first", "clear", "preserve", "hardware", "software", "mul_add",
        "multiply_add", "dsp_mac", "optional_yosys", "reduction", "reassociate",
        "auto", "nearest_even", "toward_zero", "floor", "away_zero", "wrap",
        "saturate", "lut", "ff", "dsp", "bram", "latency", "throughput", "ii",
        "fmax", "fmax_est", "parallelism", "depth", "candidates",
        "rising", "falling", "synchronous", "asynchronous", "active_high",
        "active_low", "unspecified",
    ),
    operators=(
        "<=>", "<-", "->", "=>", "..", "==", "!=", "<=", ">=", "<", ">",
        "&&", "||", "!", "~", "&", "|", "^", "<<", ">>", "+", "-", "*", "/",
        "=", "?", ":",
    ),
    capabilities=(
        PublicCapability(
            "scalar-datapath", "pure value", "supported", "supported",
            "supported", "supported", "M36/M38 scalar relations",
            CapabilityWitness("examples/all_syntax.zhl", "ScalarSyntax"),
            ("runtime division and general Boolean &&/|| are not hardware operators",),
        ),
        PublicCapability(
            "exact-literals-and-packed-constants", "pure value", "supported",
            "supported", "supported", "supported",
            "M36/M38 scalar relations where eligible",
            CapabilityWitness("examples/all_syntax.zhl", "AllSyntax"),
            (
                "compound expressions are never resized by context; zeros<N> "
                "and ones<N> produce exact raw bits",
            ),
        ),
        PublicCapability(
            "fixed-point", "pure value", "supported", "supported",
            "supported", "supported", "bounded M36/M38 relations",
            CapabilityWitness("examples/all_syntax.zhl", "FixedSyntax"),
            ("rescale, rounding, and overflow remain explicit",),
        ),
        PublicCapability(
            "aggregates", "pure value", "supported", "supported",
            "supported", "supported", "value relations only where bindable",
            CapabilityWitness("examples/all_syntax.zhl", "StructSyntax"),
            ("runtime reshape and enum representation casts fail closed",),
        ),
        PublicCapability(
            "characters-strings-tuples", "pure value and aggregate storage",
            "bounded", "supported", "supported", "supported",
            "existing scalar/packed relations where eligible",
            CapabilityWitness("examples/all_syntax.zhl", "TextTupleSyntax"),
            (
                "fixed non-empty ASCII byte strings and structural tuple arity 2..8; "
                "no Unicode, dynamic strings, runtime tuple indexing, or nested patterns",
            ),
        ),
        PublicCapability(
            "tagged-unions", "pure value", "bounded", "supported",
            "supported", "supported", "no dedicated formal family",
            CapabilityWitness("examples/tagged_union.zhl", "TaggedUnionExample"),
            ("flat scalar fields; no generic unions, top inputs, or raw decode",),
        ),
        PublicCapability(
            "functional-datapath", "pure value", "supported", "supported",
            "supported", "supported", "scalar/fixed relations where eligible",
            CapabilityWitness("examples/all_syntax.zhl", "FunctionalSyntax"),
            ("compile-time bounded; no runtime loops or implicit reassociation",),
        ),
        PublicCapability(
            "compile-time-generation", "elaboration", "supported", "not applicable",
            "supported after specialization", "supported after specialization",
            "not applicable",
            CapabilityWitness("examples/all_syntax.zhl", "CompileTimeSyntax"),
            ("generated real values require explicit hardware quantization",),
        ),
        PublicCapability(
            "concise-exact-lowering", "elaboration and pure/sequential value syntax",
            "supported", "supported", "supported", "supported",
            "inherits the properties of the normalized typed IR",
            CapabilityWitness("examples/all_syntax.zhl", "ConciseLoweringSyntax"),
            (
                "contextual resize still requires an explicit typed boundary; "
                "static ranges and parameter defaults remain compile-time only",
            ),
        ),
        PublicCapability(
            "typed-static-parameters", "elaboration", "bounded", "not applicable",
            "supported after specialization", "supported after specialization",
            "identity/cache participation; no new property family",
            CapabilityWitness(
                "tests/fixtures/generic_table_parameters.zhl",
                "GenericTableParametersCapability",
            ),
            (
                "named exact bit-packable constants and pure named functions only; "
                "no defaults, closures, runtime dispatch, ports, or state",
            ),
        ),
        PublicCapability(
            "generic-rom-and-table-gather", "elaboration and storage", "bounded",
            "supported", "supported", "supported", "existing storage safety only",
            CapabilityWitness(
                "tests/fixtures/generic_table_parameters.zhl",
                "GenericTableParametersCapability",
            ),
            (
                "immutable one-cycle ROM images and range-proven gather; gather "
                "does not prove permutation bijection",
            ),
        ),
        PublicCapability(
            "sequential-state", "single clock domain", "supported", "supported",
            "supported", "supported",
            "existing M35 register/rule families retain one outer rule-fire observation",
            CapabilityWitness("examples/all_syntax.zhl", "VectorStateUpdateSyntax"),
            (
                "recursive when/else when/else retains one Rule and ActionGroup; "
                "it is runtime atomic effect selection, not compile-time if or "
                "procedural control flow",
                "an illegal selected FIFO/memory action suppresses the complete "
                "group without readiness-selected fallback; active output writes "
                "participate in whole-rule conflict scheduling",
            ),
        ),
        PublicCapability(
            "physical-clock-reset", "single physical domain", "bounded",
            "supported", "supported", "supported",
            (
                "existing formal routes support exact rising/falling, "
                "synchronous/raw-asynchronous, polarity, and synchronized-release "
                "contracts when power_up is unspecified"
            ),
            CapabilityWitness("examples/all_syntax.zhl", "AsyncResetSyntax"),
            (
                "default synchronous active-high, raw asynchronous compatibility, "
                "or asynchronous assertion with fixed two-edge synchronized release; "
                "no power-on reset, implicit reset crossing, or multi-domain async reset",
            ),
        ),
        PublicCapability(
            "encoded-enums-and-fsm", "single clock domain", "supported", "supported",
            "supported", "supported", "no enum/FSM-specific property family",
            CapabilityWitness("examples/all_syntax.zhl", "FsmSyntax"),
            ("FSM syntax lowers to enum state plus ordinary rules",),
        ),
        PublicCapability(
            "vector-state-update", "single clock domain", "bounded", "supported",
            "supported", "supported", "register safety where observable",
            CapabilityWitness("examples/all_syntax.zhl", "VectorStateUpdateSyntax"),
            ("one range-proven element write; no nested paths or runtime-selected instances",),
        ),
        PublicCapability(
            "fifo-storage", "single clock domain", "supported", "supported",
            "supported", "supported", "existing M35 FIFO family",
            CapabilityWitness("examples/all_syntax.zhl", "FifoSyntax"),
            ("bounded synchronous FIFO semantics",),
        ),
        PublicCapability(
            "writable-memory", "single clock domain", "bounded", "supported",
            "supported", "supported", "no memory-specific M35 family",
            CapabilityWitness("examples/ztpu_async_memory.zhl", "ZtpuAsyncMemory"),
            (
                "arbitrary-width bit-packable 1R1W; global reads may be "
                "combinational or one-cycle; byte masks use ceil(W/8) lanes "
                "and clip the final high lane; "
                "scheduled reads remain one-cycle; cell and read-result reset "
                "policies are independent; no initialized or native multiport "
                "writable memory; bounded replicated/banked ports are ordinary "
                "source hierarchy",
            ),
        ),
        PublicCapability(
            "ready-valid", "protocol", "supported", "supported",
            "supported", "supported", "existing M35 ready/valid family",
            CapabilityWitness("examples/all_syntax.zhl", "ReadyValidSyntax"),
            ("no implicit buffering, adaptation, or CDC",),
        ),
        PublicCapability(
            "credit", "protocol", "supported", "supported",
            "supported", "supported",
            "existing M35 sender/receiver credit family when the exact counter is bound",
            CapabilityWitness("examples/all_syntax.zhl", "CreditSyntax"),
            (
                "sender credits and receiver adapter occupancy are explicit; "
                "VC-credit has no M35 accounting family and protocol-level M38 "
                "is not claimed",
            ),
        ),
        PublicCapability(
            "request-response", "protocol", "supported", "supported",
            "supported", "supported",
            "existing M35 ledger and directional-buffer safety family",
            CapabilityWitness("examples/all_syntax.zhl", "RequestResponseSyntax"),
            (
                "parent outstanding and request/response buffer occupancies are "
                "typed formal observations; bounded in-order/out-of-order profiles; "
                "no protocol-level M38",
            ),
        ),
        PublicCapability(
            "aggregate-protocols", "protocol hierarchy", "bounded", "supported",
            "supported", "supported", "no general aggregate property family",
            CapabilityWitness("examples/all_syntax.zhl", "AggregateProtocolSyntax"),
            (
                "typed leaves and explicit ownership; adapters/crossings remain "
                "explicit; scalar M36/M38 entry points reject aggregate protocol "
                "endpoints rather than comparing leaves out of context",
            ),
        ),
        PublicCapability(
            "ahb-lite-stdlib", "source-authored standard bus", "bounded",
            "supported", "supported", "supported",
            "existing register/state and ready-valid properties where bindable",
            CapabilityWitness("examples/ahb_csr_top.zhl", "AhbCsrTop"),
            (
                "single-manager, one-outstanding, aligned full-width transfers for "
                "power-of-two data widths 8..1024; no subword strobes, burst engine, "
                "multi-manager arbitration, CDC, or AHB-specific formal family",
            ),
        ),
        PublicCapability(
            "cdc", "multiple clock domains", "bounded", "supported",
            "supported", "supported", "no CDC proof family",
            CapabilityWitness("examples/all_syntax.zhl", "CdcSyntax"),
            ("only frozen sync_level, pulse_toggle, handshake, and async_fifo crossings",),
        ),
        PublicCapability(
            "csr", "single clock domain", "supported", "supported",
            "supported", "supported", "existing M35 CSR family",
            CapabilityWitness("examples/all_syntax.zhl", "CsrSyntax"),
            ("source-authored bounded register-bank model",),
        ),
        PublicCapability(
            "contracts", "verification", "supported", "supported",
            "artifact generation", "artifact generation",
            "M35 safety and bounded cover execution when bound",
            CapabilityWitness(
                "editors/vscode/zlang-hdl/examples/verification.zhl",
                "VerificationUxSyntax",
            ),
            (
                "clocked same-cycle safety and bounded reachability only; "
                "per-goal supported clock/reset routing does not imply cross-domain "
                "proof semantics; liveness, temporal sequences, and source M36/M38 "
                "controls are deferred",
            ),
        ),
        PublicCapability(
            "exploration", "implementation selection", "bounded", "not applicable",
            "selected candidates", "selected candidates",
            "M39 `available` is advisory; required policies require connected M36",
            CapabilityWitness("examples/all_syntax.zhl", "ExplorationSyntax"),
            (
                "egglog, architecture enumeration, pipeline planning, cost "
                "extraction, and proof gating are distinct stages; joint "
                "--verify may add direct M36/M38 evidence, but M38 never gates M39",
            ),
        ),
        PublicCapability(
            "elastic-ready-valid-pipeline",
            "single-domain ready/valid transform",
            "bounded",
            "supported",
            "supported",
            "supported",
            "existing M35 ready/valid stability only; M36/M38 unsupported",
            CapabilityWitness(
                "examples/elastic_pipeline_auto.zhl", "ElasticPipelineAuto"
            ),
            (
                "one pure M31 product-reduction kernel, global stall, II=1 and "
                "variable wall-clock latency; no user state or independently "
                "elastic stages; minimum unstalled latency is not a fixed-latency "
                "equivalence relation",
            ),
        ),
        PublicCapability(
            "combinational-instance-arrays", "compile-time hierarchy", "bounded",
            "supported", "supported", "supported", "no hierarchical M36/M38",
            CapabilityWitness("examples/indexed_instance_array.zhl", "IndexedInstanceArray"),
            (
                "one-dimensional and compile-time indexed; exact aggregate wire "
                "outputs and output-only runtime projection are supported; no "
                "runtime-selected input, protocol, action, or physical identity",
            ),
        ),
        PublicCapability(
            "runtime-instance-output-projection", "elaborated hierarchy", "bounded",
            "supported", "supported", "supported", "no hierarchical M36/M38",
            CapabilityWitness(
                "tests/fixtures/hierarchy/runtime_selected_instance_output.zhl",
                "RuntimeSelectedInstanceOutputCapability",
            ),
            (
                "read-only exact bit-packable wire outputs; every physical child "
                "continues to execute independently",
            ),
        ),
        PublicCapability(
            "sequential-instance-arrays", "compile-time hierarchy", "bounded",
            "supported", "supported", "supported", "no new recursive observation family",
            CapabilityWitness("examples/sequential_instance_array.zhl", "StateLaneArray"),
            (
                "one matching domain; bounded nested scalar hierarchy preserves "
                "each physical child identity",
            ),
        ),
        PublicCapability(
            "storage-instance-arrays", "compile-time hierarchy", "bounded",
            "supported", "supported", "supported", "no hidden-cell observations",
            CapabilityWitness("examples/storage_instance_array.zhl", "FifoLaneArray"),
            (
                "one FIFO, memory, or ROM per child; scheduled FIFO actions may "
                "share the existing transition with ordinary register/rule state, "
                "while legacy global storage plus user state fails closed; two flat "
                "memory arrays may form replicated banked read ports without a "
                "native multiport primitive",
            ),
        ),
        PublicCapability(
            "ready-valid-instance-arrays", "compile-time protocol hierarchy", "bounded",
            "supported", "supported", "supported", "existing applicable safety only",
            CapabilityWitness("examples/rv_fifo_instance_array.zhl", "RvBufferedLaneArray"),
            (
                "primitive ready/valid may coexist with scalar wire ports and one "
                "FIFO; bounded direct same-domain nested ready/valid is supported; "
                "no CDC",
            ),
        ),
        PublicCapability(
            "request-response-instance-arrays",
            "compile-time protocol hierarchy",
            "bounded",
            "supported",
            "supported",
            "supported",
            "existing applicable safety only",
            CapabilityWitness(
                "tests/fixtures/hierarchy/request_response_instance_array.zhl",
                "RequestResponseArrayTop",
            ),
            (
                "first-level, one-dimensional, in-order requester/responder "
                "arrays with scalar side ports; no out-of-order or nested "
                "request/response arrays",
            ),
        ),
    ),
    documentation=(
        DocumentationRequirement("modules", "docs/syntax-support-matrix.md", ("`module`", "`in`/`out`")),
        DocumentationRequirement("scalars", "docs/syntax-support-matrix.md", ("`bit`", "`uint<N>`")),
        DocumentationRequirement("fixed-point", "docs/syntax-support-matrix.md", ("`fixed<W,F>`", "`SF8.8`")),
        DocumentationRequirement(
            "aggregates",
            "docs/syntax-support-matrix.md",
            (
                "`vec<N,T>`",
                "structs",
                "`value with { ... }`",
                "`Struct { ... } = value`",
                "`repeat(value)`",
            ),
        ),
        DocumentationRequirement(
            "packing",
            "docs/syntax-support-matrix.md",
            ("`x[MSB:LSB]`", "`bits_value[i]`", "`concat`, `reshape`, `bitcast`"),
        ),
        DocumentationRequirement(
            "exact-literals-and-packed-constants",
            "docs/types-and-numerics.md",
            ("smallest exact hardware type", "zeros<24>", "ones<24>"),
        ),
        DocumentationRequirement(
            "characters-strings-tuples",
            "docs/types-and-numerics.md",
            (
                "`char` is a source alias for canonical `u8`",
                "`string<N>` is a source alias",
                "canonical `vec<N,u8>`",
                "Tuple types use `(T,U)`",
            ),
        ),
        DocumentationRequirement("enums", "docs/syntax-support-matrix.md", ("`enum State { ... }`", "exhaustive enum `switch`")),
        DocumentationRequirement(
            "tagged-unions",
            "docs/syntax-support-matrix.md",
            ("`union Message { ... }`", "exhaustive pure `match`"),
        ),
        DocumentationRequirement("functions", "docs/syntax-support-matrix.md", ("pure `fn`", "generic structs/functions/operators")),
        DocumentationRequirement("expressions", "docs/syntax-support-matrix.md", ("`?:`", "`switch`")),
        DocumentationRequirement("functional-datapath", "docs/syntax-support-matrix.md", ("`generate`", "`dot`")),
        DocumentationRequirement("compile-time", "docs/syntax-support-matrix.md", ("compile-time `if`", "bounded specialization-time")),
        DocumentationRequirement(
            "typed-static-parameters",
            "docs/expressions-functions-generics.md",
            ("Typed constants", "operation : fn(A) -> B", "named"),
        ),
        DocumentationRequirement(
            "state",
            "docs/syntax-support-matrix.md",
            (
                "runtime-indexed `reg vec` element update",
                "`else when`",
                "one `Rule` and one `ActionGroup`",
                "does not fall back",
                "`priority`",
                "`fsm state : Enum = Initial { ... }`",
            ),
        ),
        DocumentationRequirement(
            "runtime-atomic-actions",
            "docs/sequential-state-storage.md",
            (
                "Every guard and operand reads the same pre-edge snapshot",
                "one outer `Rule`",
                "one `ActionGroup`",
                "not fall back to an `else` branch",
                "Scalar output writes are scheduling resources",
                "compile-time `if`",
            ),
        ),
        DocumentationRequirement(
            "module-timing",
            "docs/syntax-support-matrix.md",
            ("`timing { latency N ii 1 }`", "immutable public behavior"),
        ),
        DocumentationRequirement(
            "physical-clock-reset",
            "docs/physical-clock-reset-contract.md",
            (
                "`ClockDomain`",
                "async reset",
                "two active clock edges",
                "mode asynchronous",
            ),
        ),
        DocumentationRequirement(
            "named-module-interfaces",
            "docs/named-module-interfaces.md",
            ("`module M : Ifc`", "exact, behavior-free module", "automatic substitution"),
        ),
        DocumentationRequirement(
            "typed-external-modules",
            "docs/hierarchy-protocols.md",
            ("`extern module VendorAdd : AddIfc { model add_model }`", "ExternalPhysicalMapping"),
        ),
        DocumentationRequirement(
            "storage",
            "docs/syntax-support-matrix.md",
            (
                "`fifo`, `memory`, initialized `rom`",
                "global memory reads are zero- or one-cycle",
                "optional `ceil(W/8)` byte write mask",
                "cell and read-result reset are independently `clear`/`preserve`",
            ),
        ),
        DocumentationRequirement(
            "generic-rom-and-table-gather",
            "docs/stdlib.md",
            ("`StorageRom`", "`StorageGeneratedRom`", "`table_gather<T,N,IW>`"),
        ),
        DocumentationRequirement("protocols", "docs/syntax-support-matrix.md", ("ready/valid, credit, request/response",)),
        DocumentationRequirement(
            "ahb-lite-stdlib",
            "docs/standard-bus-library.md",
            ("`std.bus.ahb_lite`", "two-cycle ERROR", "aligned, full-bus-width"),
        ),
        DocumentationRequirement(
            "composition",
            "docs/syntax-support-matrix.md",
            ("`connect`, option-free named-transform chains, adapters, arbitration, CDC",),
        ),
        DocumentationRequirement(
            "instance-arrays",
            "docs/syntax-support-matrix.md",
            (
                "aggregate scalar outputs",
                "scheduled FIFO plus ordinary rule/register state",
                "bounded nested same-domain scalar/direct-ready-valid hierarchy",
                "first-level in-order request/response arrays",
                "`lane[select].output`",
            ),
        ),
        DocumentationRequirement(
            "elastic-ready-valid-pipeline",
            "docs/optimization-formal.md",
            ("transform pipeline(auto", "global-clock-enable", "variable wall-clock"),
        ),
        DocumentationRequirement("csr", "docs/syntax-support-matrix.md", ("CSR access", "`w1c`")),
        DocumentationRequirement(
            "contracts",
            "docs/syntax-support-matrix.md",
            (
                "named `assert`/`cover`",
                "`contract` with `require`/`assert`/`ensure`/`cover`",
                "legacy `assume`/`guarantee`",
            ),
        ),
        DocumentationRequirement("rewrites", "docs/syntax-support-matrix.md", ("`equiv`",)),
        DocumentationRequirement("exploration", "docs/syntax-support-matrix.md", ("`choice`, `architecture`, `explore`",)),
        DocumentationRequirement("compile-time-math", "docs/syntax-support-matrix.md", ("`pi`, `sin`, `cos`",)),
    ),
)


__all__ = [
    "CAPABILITY_REGISTRY",
    "CapabilityWitness",
    "DocumentationRequirement",
    "PublicCapability",
    "PublicCapabilityRegistry",
]
