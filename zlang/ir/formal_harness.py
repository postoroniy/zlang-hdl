"""Formal predicate rendering and executable SystemVerilog harness emission."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re

from zlang.backend.identifiers import allocate_private_rtl_identifier
from zlang.common.systemverilog import render_ordered_comparison, render_right_shift
from zlang.formal_domain import (
    FormalDomainRendering,
    FormalDomainRenderingError,
    render_formal_domain,
)
from zlang.ir.cdc import ClockDomain
from zlang.ir.formal_domains import _formal_item_domain_from_design
from zlang.ir.formal_models import (
    CoverProperty,
    FormalDesign,
    FormalError,
    FormalProperty,
    ProofMode,
    PropertyKind,
    SignalBinding,
    TemporalForm,
)
from zlang.ir.formal_predicates import (
    Binary as PredicateBinary,
    Constant as PredicateConstant,
    FormalBinaryOperator,
    FormalPredicate,
    FormalPredicateError,
    FormalSignedness,
    FormalUnaryOperator,
    Mux as PredicateMux,
    ObservationCycle,
    ObservationRef,
    Unary as PredicateUnary,
    require_predicate,
)


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _predicate_report(design: FormalDesign, *, mode: ProofMode, depth: int) -> str:
    design_reason = (
        design.non_executable_reason
        or "backend formal artifact is not connected"
    )
    lines = [
        "// ZLang safety verification non-executable property report",
        f"// module={design.module_name} mode={mode.value} depth={depth}",
        f"// reason={design_reason}",
    ]
    for item in design.properties:
        predicate = item.predicate.render() if item.predicate is not None else item.expression
        reason = item.non_executable_reason or design_reason
        lines.append(
            f"// {item.kind.value} {item.id}: {predicate} [not-run: {reason}]"
        )
    for item in design.covers:
        predicate = (
            item.predicate.render()
            if item.predicate is not None else item.expression
        )
        reason = item.non_executable_reason or design_reason
        lines.append(
            f"// cover {item.id}: {predicate} [not-run: {reason}]"
        )
    return "\n".join(lines) + "\n"


def _sv_cast(expression: str, width: int, signedness: FormalSignedness) -> str:
    if signedness is FormalSignedness.SIGNED:
        return f"$signed({width}'($signed({expression})))"
    return f"{width}'($unsigned({expression}))"


def _render_predicate(value: FormalPredicate, bindings: dict[str, SignalBinding]) -> str:
    if isinstance(value, ObservationRef):
        item = bindings.get(value.semantic_signal_id)
        if item is None:
            raise FormalError(
                f"structured predicate has no binding for '{value.semantic_signal_id}'"
            )
        token = item.rtl_name
        if not _IDENT.match(token):
            raise FormalError(
                f"formal observation token is not a legal identifier: {token!r}"
            )
        return (
            f"$past({token})"
            if value.cycle is ObservationCycle.PREVIOUS else token
        )
    if isinstance(value, PredicateConstant):
        if value.signedness is FormalSignedness.SIGNED:
            if value.value < 0:
                return f"-{value.width}'sd{abs(value.value)}"
            return f"{value.width}'sd{value.value}"
        return f"{value.width}'d{value.value}"
    if isinstance(value, PredicateUnary):
        operand = _render_predicate(value.operand, bindings)
        if value.operator is FormalUnaryOperator.LOGICAL_NOT:
            return f"!({operand})"
        if value.operator is FormalUnaryOperator.BITWISE_NOT:
            return f"~({operand})"
        return _sv_cast(operand, value.width, value.signedness)
    if isinstance(value, PredicateMux):
        condition = _render_predicate(value.condition, bindings)
        when_true = _sv_cast(
            _render_predicate(value.when_true, bindings),
            value.width,
            value.signedness,
        )
        when_false = _sv_cast(
            _render_predicate(value.when_false, bindings),
            value.width,
            value.signedness,
        )
        return f"(({condition}) ? ({when_true}) : ({when_false}))"
    if isinstance(value, PredicateBinary):
        left = _render_predicate(value.left, bindings)
        right = _render_predicate(value.right, bindings)
        symbol = {
            FormalBinaryOperator.LOGICAL_AND: "&&",
            FormalBinaryOperator.LOGICAL_OR: "||",
            FormalBinaryOperator.ADD: "+",
            FormalBinaryOperator.SUBTRACT: "-",
            FormalBinaryOperator.MULTIPLY: "*",
            FormalBinaryOperator.BIT_AND: "&",
            FormalBinaryOperator.BIT_OR: "|",
            FormalBinaryOperator.BIT_XOR: "^",
            FormalBinaryOperator.SHIFT_LEFT: "<<",
            FormalBinaryOperator.SHIFT_RIGHT: ">>",
            FormalBinaryOperator.EQUAL: "==",
            FormalBinaryOperator.NOT_EQUAL: "!=",
            FormalBinaryOperator.LESS: "<",
            FormalBinaryOperator.LESS_EQUAL: "<=",
            FormalBinaryOperator.GREATER: ">",
            FormalBinaryOperator.GREATER_EQUAL: ">=",
        }
        if value.operator is FormalBinaryOperator.IMPLIES:
            return f"(!({left}) || ({right}))"
        if value.operator in {
            FormalBinaryOperator.LOGICAL_AND,
            FormalBinaryOperator.LOGICAL_OR,
        }:
            return f"(({left}) {symbol[value.operator]} ({right}))"
        if value.operator in {
            FormalBinaryOperator.ADD,
            FormalBinaryOperator.SUBTRACT,
            FormalBinaryOperator.MULTIPLY,
            FormalBinaryOperator.BIT_AND,
            FormalBinaryOperator.BIT_OR,
            FormalBinaryOperator.BIT_XOR,
            FormalBinaryOperator.SHIFT_LEFT,
            FormalBinaryOperator.SHIFT_RIGHT,
        }:
            left = _sv_cast(left, value.width, value.signedness)
            if value.operator not in {
                FormalBinaryOperator.SHIFT_LEFT,
                FormalBinaryOperator.SHIFT_RIGHT,
            }:
                right = _sv_cast(right, value.width, value.signedness)
            if value.operator is FormalBinaryOperator.SHIFT_RIGHT:
                shifted = render_right_shift(
                    left,
                    right,
                    signed=value.signedness is FormalSignedness.SIGNED,
                )
                return _sv_cast(
                    shifted,
                    value.width,
                    value.signedness,
                )
            return _sv_cast(
                f"(({left}) {symbol[value.operator]} ({right}))",
                value.width,
                value.signedness,
            )
        operand_signedness = value.left.signedness
        operand_width = value.left.width
        left = _sv_cast(left, operand_width, operand_signedness)
        right = _sv_cast(right, operand_width, operand_signedness)
        if value.operator in {
            FormalBinaryOperator.LESS,
            FormalBinaryOperator.LESS_EQUAL,
            FormalBinaryOperator.GREATER,
            FormalBinaryOperator.GREATER_EQUAL,
        }:
            return render_ordered_comparison(
                left,
                symbol[value.operator],
                right,
                signed=operand_signedness is FormalSignedness.SIGNED,
            )
        return f"(({left}) {symbol[value.operator]} ({right}))"
    raise FormalError(f"unsupported structured predicate node: {type(value).__name__}")


def render_bound_predicate(
    value: FormalPredicate,
    bindings: dict[str, SignalBinding],
) -> str:
    """Render one typed predicate using an explicit semantic binding map.

    This is the shared conservative SystemVerilog lowering used by connected
    top-level and recursive safety verification harnesses. Keeping it public prevents backend
    adapters from reconstructing executable meaning from legacy report text.
    Every observation must be present in ``bindings``; no RTL-name fallback or
    token guessing is performed.
    """

    try:
        require_predicate(value)
    except FormalPredicateError as error:
        raise FormalError(str(error)) from error
    return _render_predicate(value, bindings)


def _canonical_dut_ports(
    ports: tuple[SignalBinding, ...],
) -> tuple[SignalBinding, ...]:
    """Return one physical DUT port per RTL token.

    BackendArtifact manifests intentionally retain both the public-port and
    typed protocol-endpoint semantic aliases for aggregate leaves.  Those
    aliases may name one exact physical RTL port, but a harness must declare
    and connect that port only once.  Alias collapsing is therefore based on
    the published physical contract, never on semantic-name spelling.

    Reusing an RTL token with a different module, width, direction, or domain
    remains a malformed artifact and fails closed.
    """

    canonical: list[SignalBinding] = []
    by_token: dict[str, SignalBinding] = {}
    for item in ports:
        previous = by_token.get(item.rtl_name)
        if previous is None:
            by_token[item.rtl_name] = item
            canonical.append(item)
            continue
        previous_contract = (
            previous.rtl_module,
            previous.width,
            previous.direction,
            previous.clock_domain,
        )
        current_contract = (
            item.rtl_module,
            item.width,
            item.direction,
            item.clock_domain,
        )
        if current_contract != previous_contract:
            def contract_text(binding: SignalBinding) -> str:
                return (
                    f"module={binding.rtl_module!r}, width={binding.width}, "
                    f"direction={binding.direction!r}, "
                    f"domain={binding.clock_domain!r}"
                )

            raise FormalError(
                "connected backend artifact reuses RTL port token "
                f"'{item.rtl_name}' with incompatible physical contracts: "
                f"'{previous.semantic_signal_id}' publishes "
                f"({contract_text(previous)}), while "
                f"'{item.semantic_signal_id}' publishes "
                f"({contract_text(item)})"
            )
    return tuple(canonical)


def _connected_domain_rendering(
    design: FormalDesign,
    items: tuple[FormalProperty | CoverProperty, ...],
    bindings: dict[str, SignalBinding],
    *,
    used_names: set[str],
) -> FormalDomainRendering:
    """Resolve and render the one exact physical domain owned by a harness."""

    clock_binding = bindings.get("clock")
    reset_binding = bindings.get("reset")
    if clock_binding is None:
        raise FormalError("connected formal harness requires a clock binding")
    if reset_binding is None:
        raise FormalError("connected formal harness requires a reset binding")
    domains: list[ClockDomain] = []
    for item in items:
        if item.non_executable_reason is not None:
            continue
        domain, reason = _formal_item_domain_from_design(item, design)
        if reason is not None or domain is None:
            raise FormalError(reason or "formal goal domain is unavailable")
        if domain not in domains:
            domains.append(domain)
    if not domains:
        if design.clock_domains:
            matches = tuple(
                item for item in design.clock_domains
                if item.clock == (clock_binding.clock_domain or item.clock)
            )
            if len(matches) == 1:
                domains.append(matches[0])
        if not domains:
            domains.append(ClockDomain(
                clock_binding.clock_domain or clock_binding.rtl_name,
                reset_binding.rtl_name,
            ))
    if len(domains) != 1:
        raise FormalError(
            "one executable formal harness cannot sample multiple physical domains"
        )
    try:
        return render_formal_domain(
            domains[0],
            clock_name=clock_binding.rtl_name,
            reset_name=reset_binding.rtl_name,
            used_names=used_names,
        )
    except FormalDomainRenderingError as error:
        raise FormalError(str(error)) from error


def _effective_reset_bindings(
    bindings: dict[str, SignalBinding],
    rendering: FormalDomainRendering,
) -> dict[str, SignalBinding]:
    """Return a predicate binding view with normalized effective reset."""

    reset = bindings.get("reset")
    if reset is None:
        return bindings
    # Recursive publication gives every concrete child-reset observation its
    # own semantic identity, while deliberately mapping it to the one physical
    # top-level reset port.  Compare that explicit physical binding instead of
    # guessing from semantic-ID or generated-name spelling: all aliases of the
    # same physical reset must observe the conditioned reset during an async
    # synchronized-release epoch.
    return {
        semantic_id: (
            replace(binding, rtl_name=rendering.reset_active)
            if (
                binding.rtl_module == reset.rtl_module
                and binding.rtl_name == reset.rtl_name
                and binding.width == reset.width
                and binding.direction == reset.direction
            )
            else binding
        )
        for semantic_id, binding in bindings.items()
    }


def _same_physical_binding(left: SignalBinding, right: SignalBinding) -> bool:
    """Return whether two semantic observations name one physical signal."""

    return (
        left.rtl_module == right.rtl_module
        and left.rtl_name == right.rtl_name
        and left.width == right.width
        and left.direction == right.direction
    )


def _previous_guard_name(
    predicate: FormalPredicate,
    bindings: dict[str, SignalBinding],
    *,
    history_valid: str,
    reset_history_valid: str | None,
) -> str | None:
    """Select the valid-history bit for one structured predicate.

    Ordinary previous-cycle values must not cross an effective reset epoch.
    Reset-epoch properties are different: they deliberately ask whether the
    *previous sampled cycle* was in reset, so they need a first-sample guard
    which is not itself cleared throughout synchronized release.
    """

    previous = tuple(
        item
        for item in predicate.observations()
        if item.cycle is ObservationCycle.PREVIOUS
    )
    if not previous:
        return None
    root_reset = bindings.get("reset")
    if root_reset is not None and reset_history_valid is not None and all(
        (bound := bindings.get(item.semantic_signal_id)) is not None
        and _same_physical_binding(bound, root_reset)
        for item in previous
    ):
        return reset_history_valid
    return history_valid


def formal_harness_domain_rendering(
    design: FormalDesign,
    *,
    top: str | None = None,
    cover: bool = False,
) -> FormalDomainRendering:
    """Resolve the exact checker-private reset signal for trace publication.

    This deliberately shares the harness emitter's allocation inputs.  Bundle
    publication can therefore retain the normalized effective reset without
    parsing generated Verilog or guessing a private RTL spelling.
    """

    if design.connected_artifact_hash is None:
        raise FormalError("formal harness domain rendering requires a connected design")
    bindings = {item.semantic_signal_id: item for item in design.bindings}
    dut_ports = _canonical_dut_ports(design.dut_ports)
    observation_tokens = {
        item.rtl_name: item for item in design.bindings
        if item.semantic_signal_id not in {"clock", "reset"}
    }
    used_names = {
        item.rtl_name for item in (*dut_ports, *observation_tokens.values())
    }
    checker_top = top or f"{design.module_name}__safety_verification_formal"
    allocate_private_rtl_identifier(
        "zlang_safety_verification_past_valid",
        semantic_identity=(
            f"{checker_top}|safety_verification|history-valid"
            if cover else f"{design.module_name}|safety_verification|history-valid"
        ),
        used=used_names,
    )
    properties: tuple[FormalProperty | CoverProperty, ...] = (
        tuple(design.properties) + tuple(design.covers)
    )
    return _connected_domain_rendering(
        design,
        properties,
        bindings,
        used_names=used_names,
    )


@dataclass
class _ConnectedHarnessPrelude:
    lines: list[str]
    bindings: dict[str, SignalBinding]
    predicate_bindings: dict[str, SignalBinding]
    rendering: FormalDomainRendering
    history_valid: str
    used_names: set[str]


def _connected_harness_prelude(
    design: FormalDesign,
    *,
    top: str,
    properties: tuple[FormalProperty | CoverProperty, ...],
) -> _ConnectedHarnessPrelude:
    """Emit the shared implementation wrapper for safety and cover harnesses."""

    assert design.connected_module is not None
    assert design.implementation_text is not None
    dut_ports = _canonical_dut_ports(design.dut_ports)
    port_by_name = {item.rtl_name: item for item in dut_ports}
    input_ports = tuple(item for item in dut_ports if item.direction == "input")
    lines = [design.implementation_text.rstrip(), "", "`default_nettype none"]
    header = f"module {top}"
    if input_ports:
        declarations = []
        for item in input_ports:
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            declarations.append(f"input wire{packed} {item.rtl_name}")
        header += "(" + ", ".join(declarations) + ");"
    else:
        header += ";"
    lines.append(header)
    for item in dut_ports:
        if item.direction == "output":
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            lines.append(f"  wire{packed} {item.rtl_name};")
    observation_tokens = {
        item.rtl_name: item for item in design.bindings
        if item.semantic_signal_id not in {"clock", "reset"}
    }
    for item in observation_tokens.values():
        if item.rtl_name not in port_by_name:
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            lines.append(f"  wire{packed} {item.rtl_name};")
    connections = [
        f".{item.rtl_name}({item.rtl_name})" for item in dut_ports
    ] + [
        f".{item.rtl_name}({item.rtl_name})"
        for item in observation_tokens.values()
        if item.rtl_name not in port_by_name
    ]
    lines.append(
        f"  {design.connected_module} dut (" + ", ".join(connections) + ");"
    )
    used_names = {
        item.rtl_name for item in (*dut_ports, *observation_tokens.values())
    }
    history_valid = allocate_private_rtl_identifier(
        "zlang_safety_verification_past_valid",
        semantic_identity=f"{top}|safety_verification|history-valid",
        used=used_names,
    )
    bindings = {item.semantic_signal_id: item for item in design.bindings}
    rendering = _connected_domain_rendering(
        design,
        properties,
        bindings,
        used_names=used_names,
    )
    predicate_bindings = _effective_reset_bindings(bindings, rendering)
    lines.extend(f"  {item}" for item in rendering.support_lines)
    lines.append(f"  {rendering.initial_assumption}")
    return _ConnectedHarnessPrelude(
        lines,
        bindings,
        predicate_bindings,
        rendering,
        history_valid,
        used_names,
    )


def emit_harness(design: FormalDesign, *, mode: ProofMode = ProofMode.BMC, depth: int = 20) -> str:
    """Emit a connected checker, or an explicit non-executable report."""
    if depth < 1:
        raise FormalError("formal depth must be positive")
    if design.connected_artifact_hash is None:
        # Preserve a useful compiler artifact without pretending that free
        # semantic wires are an executable proof harness.
        for prop in design.properties:
            if prop.non_executable_reason is not None:
                continue
            for signal in prop.relevant_signals:
                if signal not in {item.semantic_signal_id for item in design.bindings}:
                    raise FormalError(
                        f"property '{prop.id}' has no signal binding for '{signal}'"
                    )
        return _predicate_report(design, mode=mode, depth=depth)
    binding = {item.semantic_signal_id: item for item in design.bindings}
    for prop in design.properties:
        if prop.non_executable_reason is not None:
            continue
        if prop.predicate is None:
            raise FormalError(
                f"property '{prop.id}' has no structured executable predicate"
            )
        if prop.temporal_form in {
            TemporalForm.STABLE_WHILE,
            TemporalForm.BOUNDED_IMPLICATION,
        }:
            raise FormalError(
                f"property '{prop.id}' uses unsupported executable temporal form "
                f"'{prop.temporal_form.value}'"
            )
        for signal in prop.relevant_signals:
            if signal not in binding:
                raise FormalError(
                    f"property '{prop.id}' has no signal binding for '{signal}'"
                )
    harness = _connected_harness_prelude(
        design,
        top=f"{design.module_name}__safety_verification_formal",
        properties=design.properties,
    )
    lines = harness.lines
    history_valid = harness.history_valid
    rendering = harness.rendering
    predicate_binding = harness.predicate_bindings
    used_names = harness.used_names
    lines.append(f"  reg {history_valid} = 1'b0;")
    legacy = rendering.domain.is_legacy_default
    reset_history_valid = None
    if not legacy:
        reset_history_valid = allocate_private_rtl_identifier(
            "zlang_safety_verification_reset_past_valid",
            semantic_identity=f"{design.module_name}|safety_verification|reset-history-valid",
            used=used_names,
        )
        lines.append(f"  reg {reset_history_valid} = 1'b0;")
        lines.append(f"  always @({rendering.sample_event}) begin")
        lines.append(f"    {reset_history_valid} <= 1'b1;")
        lines.append("  end")
    if legacy:
        lines.append(f"  always @({rendering.sample_event}) begin")
        lines.append(f"    {history_valid} <= 1'b1;")
    else:
        lines.append(f"  always @({rendering.history_event}) begin")
        lines.append(f"    if ({rendering.external_reset_asserted}) begin")
        lines.append(f"      {history_valid} <= 1'b0;")
        if rendering.release_tracker_name is not None:
            lines.append(
                f"    end else if ({rendering.reset_active}) begin"
            )
            lines.append(f"      {history_valid} <= 1'b0;")
        lines.append("    end else begin")
        lines.append(f"      {history_valid} <= 1'b1;")
        lines.extend(["    end", "  end"])
        lines.append(f"  always @({rendering.sample_event}) begin")
    property_indent = "    "
    for prop in design.properties:
        if prop.non_executable_reason is not None:
            lines.append(
                f"{property_indent}// not-run {prop.id}: "
                f"{prop.non_executable_reason}"
            )
            continue
        assert prop.predicate is not None
        statement = "assume" if prop.kind is PropertyKind.ASSUMPTION else "assert"
        condition = render_bound_predicate(prop.predicate, predicate_binding)
        guards: list[str] = []
        previous_guard = _previous_guard_name(
            prop.predicate,
            binding,
            history_valid=history_valid,
            reset_history_valid=reset_history_valid,
        )
        if previous_guard is not None:
            guards.append(previous_guard)
        if prop.reset_condition is not None:
            guards.append(f"!{rendering.reset_active}")
        guard = " && ".join(guards)
        if guard:
            lines.append(
                f"{property_indent}if ({guard}) {statement} ({condition}); "
                f"// {prop.id}"
            )
        else:
            lines.append(
                f"{property_indent}{statement} ({condition}); // {prop.id}"
            )
    lines.extend(["  end", "endmodule", "`default_nettype wire", ""])
    return "\n".join(lines)


def cover_harness_top(design: FormalDesign, cover_id: str) -> str:
    """Return the stable wrapper name for one per-goal cover harness."""

    if not cover_id:
        raise FormalError("cover harness requires a property id")
    digest = hashlib.sha256(cover_id.encode()).hexdigest()[:12]
    return f"{design.module_name}__safety_verification_cover_{digest}"


def emit_cover_harness(
    design: FormalDesign,
    *,
    cover_id: str,
    depth: int = 20,
    top: str | None = None,
) -> str:
    """Emit one connected bounded-reachability checker.

    One goal per harness keeps SBY's pass/fail cover result unambiguous.  A
    connected implementation and the exact backend-published observation
    bindings are mandatory for executable output; an unconnected design emits
    the same explicit non-executable report used by safety properties.
    """

    if depth < 1:
        raise FormalError("formal depth must be positive")
    matches = tuple(item for item in design.covers if item.id == cover_id)
    if len(matches) != 1:
        if not matches:
            raise FormalError(f"unknown cover property: {cover_id}")
        raise FormalError(f"duplicate cover property id: {cover_id}")
    prop = matches[0]
    if design.connected_artifact_hash is None:
        if prop.non_executable_reason is None:
            available = {item.semantic_signal_id for item in design.bindings}
            for signal in prop.relevant_signals:
                if signal not in available:
                    raise FormalError(
                        f"cover property '{prop.id}' has no signal binding for '{signal}'"
                    )
        return _predicate_report(design, mode=ProofMode.BMC, depth=depth)
    if prop.non_executable_reason is not None:
        return _predicate_report(
            replace(
                design,
                non_executable_reason=prop.non_executable_reason,
            ),
            mode=ProofMode.BMC,
            depth=depth,
        )
    if prop.predicate is None:
        raise FormalError(
            f"cover property '{prop.id}' has no structured executable predicate"
        )

    binding = {item.semantic_signal_id: item for item in design.bindings}
    for signal in prop.relevant_signals:
        if signal not in binding:
            raise FormalError(
                f"cover property '{prop.id}' has no signal binding for '{signal}'"
            )
    assumptions = tuple(
        item for item in design.properties
        if item.kind is PropertyKind.ASSUMPTION
    )
    for assumption in assumptions:
        if assumption.non_executable_reason is not None:
            return _predicate_report(
                replace(
                    design,
                    non_executable_reason=(
                        f"cover property '{prop.id}' requires executable assumption "
                        f"'{assumption.id}': {assumption.non_executable_reason}"
                    ),
                ),
                mode=ProofMode.BMC,
                depth=depth,
            )
        if assumption.predicate is None:
            raise FormalError(
                f"assumption '{assumption.id}' has no structured executable predicate"
            )
        if assumption.temporal_form in {
            TemporalForm.STABLE_WHILE,
            TemporalForm.BOUNDED_IMPLICATION,
        }:
            raise FormalError(
                f"assumption '{assumption.id}' uses unsupported executable temporal "
                f"form '{assumption.temporal_form.value}'"
            )
        for signal in assumption.relevant_signals:
            if signal not in binding:
                raise FormalError(
                    f"assumption '{assumption.id}' has no signal binding for '{signal}'"
                )
    top = top or cover_harness_top(design, cover_id)
    if not _IDENT.match(top):
        raise FormalError(f"cover harness top is not a legal identifier: {top!r}")

    harness = _connected_harness_prelude(
        design,
        top=top,
        properties=(*assumptions, prop),
    )
    lines = harness.lines
    history_valid = harness.history_valid
    rendering = harness.rendering
    predicate_binding = harness.predicate_bindings
    used_names = harness.used_names
    cover_needs_past = any(
        item.cycle is ObservationCycle.PREVIOUS
        for item in prop.predicate.observations()
    )
    needs_past = cover_needs_past or any(
        observation.cycle is ObservationCycle.PREVIOUS
        for assumption in assumptions
        if assumption.predicate is not None
        for observation in assumption.predicate.observations()
    )
    if needs_past:
        lines.append(f"  reg {history_valid} = 1'b0;")
    legacy = rendering.domain.is_legacy_default
    reset_history_valid = None
    if needs_past and not legacy:
        reset_history_valid = allocate_private_rtl_identifier(
            "zlang_safety_verification_reset_past_valid",
            semantic_identity=f"{top}|safety_verification|reset-history-valid",
            used=used_names,
        )
        lines.append(f"  reg {reset_history_valid} = 1'b0;")
        lines.append(f"  always @({rendering.sample_event}) begin")
        lines.append(f"    {reset_history_valid} <= 1'b1;")
        lines.append("  end")
    if needs_past and not legacy:
        lines.append(f"  always @({rendering.history_event}) begin")
        lines.append(f"    if ({rendering.external_reset_asserted}) begin")
        lines.append(f"      {history_valid} <= 1'b0;")
        if rendering.release_tracker_name is not None:
            lines.append(
                f"    end else if ({rendering.reset_active}) begin"
            )
            lines.append(f"      {history_valid} <= 1'b0;")
        lines.append("    end else begin")
        lines.append(f"      {history_valid} <= 1'b1;")
        lines.extend(["    end", "  end"])
    lines.append(f"  always @({rendering.sample_event}) begin")
    statement_indent = "    "
    if needs_past and legacy:
        lines.append(f"    {history_valid} <= 1'b1;")
    for assumption in assumptions:
        if assumption.non_executable_reason is not None:
            lines.append(
                f"    // not-run {assumption.id}: "
                f"{assumption.non_executable_reason}"
            )
            continue
        assert assumption.predicate is not None
        assumption_condition = render_bound_predicate(
            assumption.predicate, predicate_binding
        )
        assumption_guards: list[str] = []
        previous_guard = _previous_guard_name(
            assumption.predicate,
            binding,
            history_valid=history_valid,
            reset_history_valid=reset_history_valid,
        )
        if previous_guard is not None:
            assumption_guards.append(previous_guard)
        if assumption.reset_condition is not None:
            assumption_guards.append(f"!{rendering.reset_active}")
        assumption_guard = " && ".join(assumption_guards)
        if assumption_guard:
            lines.append(
                f"{statement_indent}if ({assumption_guard}) "
                f"assume ({assumption_condition}); "
                f"// {assumption.id}"
            )
        else:
            lines.append(
                f"{statement_indent}assume ({assumption_condition}); "
                f"// {assumption.id}"
            )
    condition = render_bound_predicate(prop.predicate, predicate_binding)
    guards: list[str] = []
    if cover_needs_past:
        previous_guard = _previous_guard_name(
            prop.predicate,
            binding,
            history_valid=history_valid,
            reset_history_valid=reset_history_valid,
        )
        assert previous_guard is not None
        guards.append(previous_guard)
    if prop.reset_condition is not None:
        guards.append(f"!{rendering.reset_active}")
    guard = " && ".join(guards)
    if guard:
        lines.append(
            f"{statement_indent}if ({guard}) cover ({condition}); // {prop.id}"
        )
    else:
        lines.append(f"{statement_indent}cover ({condition}); // {prop.id}")
    lines.extend(["  end", "endmodule", "`default_nettype wire", ""])
    return "\n".join(lines)
