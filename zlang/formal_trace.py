"""Shared, fail-closed VCD decoding for existing formal result families.

The decoder is intentionally independent of M35/M36/M38 result schemas.  It
maps physical VCD leaves through explicit binding metadata, retains unknown
four-state values verbatim, and only applies a typed interpretation when the
caller supplies an exact canonical hardware type.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
import re
from typing import Iterable

from zlang.ir.comparison_window import ComparisonWindow, ComparisonWindowKind
from zlang.ir.packing import PackingError, unpack_runtime
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedOverflowPolicy,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
    VecType,
)


@dataclass(frozen=True)
class TraceBinding:
    """One explicit semantic-to-VCD leaf mapping.

    ``canonical_type`` may be omitted when an older artifact publishes only a
    width.  Such values remain raw instead of being guessed from a signal name.
    """

    semantic_signal_id: str
    rtl_name: str
    width: int
    canonical_type: HardwareType | str | None = None
    signedness: str | None = None

    def __post_init__(self) -> None:
        if not self.semantic_signal_id or not self.rtl_name:
            raise ValueError("formal trace bindings require semantic and RTL names")
        if self.width < 1:
            raise ValueError("formal trace binding width must be positive")
        if self.signedness not in {None, "bit", "bits", "signed", "unsigned"}:
            raise ValueError("formal trace binding signedness is invalid")


@dataclass(frozen=True)
class FormalTraceSnapshot:
    failure_cycle: int | None
    sample_cycle: int | None
    reset_state: str | None
    comparison_valid_state: str | None
    values: tuple[tuple[str, str], ...]
    trace_path: str | None = None


def _split_top_level(text: str) -> tuple[str, ...]:
    result: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(text):
        if character in "<(":
            depth += 1
        elif character in ">)":
            depth -= 1
        elif character == "," and depth == 0:
            result.append(text[start:index].strip())
            start = index + 1
    result.append(text[start:].strip())
    return tuple(result)


def _canonical_scalar_type(text: str) -> HardwareType | None:
    """Restore only canonical spellings whose full identity is in the text."""

    if text == "bit":
        return BitType()
    match = re.fullmatch(r"u([1-9][0-9]*)", text)
    if match:
        return UIntType(int(match.group(1)))
    match = re.fullmatch(r"s([1-9][0-9]*)", text)
    if match:
        return SIntType(int(match.group(1)))
    match = re.fullmatch(r"bits<([1-9][0-9]*)>", text)
    if match:
        return BitsType(int(match.group(1)))
    match = re.fullmatch(
        r"(u?fixed(?:_sat)?)<([1-9][0-9]*),([0-9]+)>", text
    )
    if match:
        family, width, fraction = match.groups()
        overflow = (
            FixedOverflowPolicy.SATURATE
            if family.endswith("_sat")
            else FixedOverflowPolicy.WRAP
        )
        constructor = UFixedType if family.startswith("u") else FixedType
        try:
            return constructor(int(width), int(fraction), overflow)
        except ValueError:
            return None
    match = re.fullmatch(r"vec<([1-9][0-9]*),(.+)>", text)
    if match:
        element = _canonical_scalar_type(match.group(2))
        return None if element is None else VecType(int(match.group(1)), element)
    if text.startswith("(") and text.endswith(")"):
        # Tuple restoration is deliberately omitted here: a structural tuple
        # can be decoded only when every nested type is available as typed IR.
        return None
    if text.startswith("enum<") and text.endswith(">"):
        body = text[5:-1]
        declaration, separator, identity = body.rpartition("@")
        name, colon, members_text = declaration.partition(":")
        if not separator or not colon or not name or not identity:
            return None
        explicit_width: int | None = None
        width_match = re.search(r":bits<([1-9][0-9]*)>$", members_text)
        if width_match:
            explicit_width = int(width_match.group(1))
            members_text = members_text[:width_match.start()]
        members = _split_top_level(members_text)
        if not members or any(not item for item in members):
            return None
        try:
            if explicit_width is None:
                return EnumType(name, members, identity)
            names: list[str] = []
            codes: list[int] = []
            for item in members:
                member, equals, code = item.partition("=")
                if not equals:
                    return None
                names.append(member)
                codes.append(int(code, 10))
            return EnumType(
                name, tuple(names), identity, explicit_width, tuple(codes)
            )
        except (TypeError, ValueError):
            return None
    return None


def _known_raw(value: str, width: int) -> int | None:
    raw = value.removeprefix("0b")
    if not raw or not set(raw) <= {"0", "1"}:
        return None
    number = int(raw, 2)
    return number if number < (1 << width) else None


def _fraction_text(value: int, fraction: int) -> str:
    exact = Fraction(value, 1 << fraction)
    return str(exact.numerator) if exact.denominator == 1 else (
        f"{exact.numerator}/{exact.denominator}"
    )


def _typed_runtime_value(type_: HardwareType, raw: int) -> object:
    if isinstance(type_, BitsType):
        return f"0b{raw:0{type_.width}b}"
    if isinstance(type_, EnumType):
        if raw not in type_.codes:
            return {"enum": type_.name, "invalid_code": raw}
        index = type_.codes.index(raw)
        return {
            "enum": f"{type_.name}.{type_.members[index]}",
            "code": raw,
        }
    value = unpack_runtime(type_, raw)
    if isinstance(type_, (FixedType, UFixedType)):
        assert isinstance(value, int)
        return {
            "type": str(type_),
            "raw": value,
            "value": _fraction_text(value, type_.fraction),
        }
    return value


def _render_value(binding: TraceBinding, raw_value: str) -> str:
    type_ = binding.canonical_type
    if isinstance(type_, str):
        type_ = _canonical_scalar_type(type_)
    raw = _known_raw(raw_value, binding.width)
    if raw is None:
        return raw_value
    if type_ is not None and type_.width == binding.width:
        try:
            value = _typed_runtime_value(type_, raw)
        except (PackingError, TypeError, ValueError):
            return raw_value
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        return str(value)
    if binding.signedness is None:
        return raw_value
    if binding.signedness == "signed" and raw >= 1 << (binding.width - 1):
        return str(raw - (1 << binding.width))
    return str(raw)


def originating_sample_cycle(
    failure_cycle: int | None,
    comparison_window: ComparisonWindow | None,
) -> int | None:
    if failure_cycle is None or comparison_window is None:
        return None
    if comparison_window.kind is ComparisonWindowKind.SAME_CYCLE:
        return failure_cycle
    if failure_cycle < comparison_window.first_comparison_cycle:
        return None
    return failure_cycle - comparison_window.fill_cycles


def decode_vcd_trace(
    path: Path,
    *,
    cycle: int | None,
    bindings: Iterable[TraceBinding],
    comparison_window: ComparisonWindow | None = None,
) -> FormalTraceSnapshot:
    """Decode one SBY trace frame through exact, caller-owned bindings."""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return FormalTraceSnapshot(cycle, None, None, None, (), None)

    scopes: list[str] = []
    symbols: dict[str, tuple[str, str, int]] = {}
    step_symbol: str | None = None
    definition_end = 0
    for definition_end, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("$scope "):
            parts = stripped.split()
            if len(parts) >= 3:
                scopes.append(parts[2])
        elif stripped.startswith("$upscope"):
            if scopes:
                scopes.pop()
        elif stripped.startswith("$var "):
            parts = stripped.split()
            if len(parts) >= 6:
                try:
                    width = int(parts[2])
                except ValueError:
                    continue
                symbol, leaf = parts[3], parts[4]
                symbols[symbol] = (".".join((*scopes, leaf)), leaf, width)
                if leaf == "smt_step":
                    step_symbol = symbol
        elif stripped.startswith("$enddefinitions"):
            break
    if step_symbol is None:
        return FormalTraceSnapshot(cycle, None, None, None, (), str(path))

    current: dict[str, str] = {}
    selected: dict[str, str] | None = None
    selected_step: int | None = None
    current_step: int | None = None

    def finish_frame() -> None:
        nonlocal selected, selected_step
        if current_step is None:
            return
        if cycle is None:
            if selected_step is None or current_step >= selected_step:
                selected, selected_step = dict(current), current_step
        elif current_step == cycle:
            selected, selected_step = dict(current), current_step

    for line in lines[definition_end + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            finish_frame()
            continue
        if stripped[0] in "01xXzZ":
            value, symbol = stripped[0].lower(), stripped[1:]
        elif stripped[0] in "bB":
            parts = stripped[1:].split(None, 1)
            if len(parts) != 2:
                continue
            value, symbol = "0b" + parts[0].lower(), parts[1]
        else:
            continue
        current[symbol] = value
        if symbol == step_symbol:
            raw = value.removeprefix("0b")
            if raw and set(raw) <= {"0", "1"}:
                current_step = int(raw, 2)
    finish_frame()
    failure_cycle = selected_step if selected_step is not None else cycle
    if selected is None:
        return FormalTraceSnapshot(
            failure_cycle,
            originating_sample_cycle(failure_cycle, comparison_window),
            None,
            None,
            (),
            str(path),
        )

    values: list[tuple[str, str]] = []
    rendered_by_semantic: dict[str, str] = {}
    for binding in sorted(bindings, key=lambda item: item.semantic_signal_id):
        candidates = [
            symbol
            for symbol, (full, leaf, width) in symbols.items()
            if (
                (leaf == binding.rtl_name if "." not in binding.rtl_name else (
                    full == binding.rtl_name
                    or full.endswith("." + binding.rtl_name)
                ))
            )
            and width == binding.width
            and symbol in selected
        ]
        candidate_values = {selected[symbol] for symbol in candidates}
        # Multiple aliases are attributable only when all physical copies agree.
        if len(candidate_values) != 1:
            continue
        raw_value = next(iter(candidate_values))
        rendered = _render_value(binding, raw_value)
        rendered_by_semantic[binding.semantic_signal_id] = rendered
        values.append((binding.semantic_signal_id, rendered))

    return FormalTraceSnapshot(
        failure_cycle,
        originating_sample_cycle(failure_cycle, comparison_window),
        rendered_by_semantic.get("trace:reset")
        or rendered_by_semantic.get("reset"),
        rendered_by_semantic.get("comparison_valid")
        or rendered_by_semantic.get("trace:comparison_valid"),
        tuple(values),
        str(path),
    )


__all__ = [
    "FormalTraceSnapshot",
    "TraceBinding",
    "decode_vcd_trace",
    "originating_sample_cycle",
]
