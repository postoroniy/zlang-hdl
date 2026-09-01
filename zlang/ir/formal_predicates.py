"""Small backend-independent value/predicate IR for frozen M35 properties.

The nodes in this module contain semantic observation IDs only.  They never
contain RTL identifiers, and their serialized representation is deliberately
strict so malformed or newer predicate forms cannot be executed accidentally.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, Mapping, Self


FORMAL_PREDICATE_SCHEMA = "zlang-formal-predicate-v1"


class FormalPredicateError(ValueError):
    """A structured formal value is malformed or unsupported."""


class FormalSignedness(str, Enum):
    BIT = "bit"
    UNSIGNED = "unsigned"
    SIGNED = "signed"
    BITS = "bits"


class ObservationCycle(str, Enum):
    """Sampling point supported by the frozen one-cycle M35 subset."""

    CURRENT = "current"
    PREVIOUS = "previous"


class FormalUnaryOperator(str, Enum):
    LOGICAL_NOT = "logical_not"
    BITWISE_NOT = "bitwise_not"
    RESIZE = "resize"


class FormalBinaryOperator(str, Enum):
    LOGICAL_AND = "logical_and"
    LOGICAL_OR = "logical_or"
    IMPLIES = "implies"
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    BIT_AND = "bit_and"
    BIT_OR = "bit_or"
    BIT_XOR = "bit_xor"
    SHIFT_LEFT = "shift_left"
    SHIFT_RIGHT = "shift_right"
    EQUAL = "equal"
    NOT_EQUAL = "not_equal"
    LESS = "less"
    LESS_EQUAL = "less_equal"
    GREATER = "greater"
    GREATER_EQUAL = "greater_equal"


class FormalPredicate:
    """Base class for a typed structured formal value.

    A property predicate is a value with ``width == 1`` and signedness
    :attr:`FormalSignedness.BIT`; intermediate values may have wider types.
    """

    width: int
    signedness: FormalSignedness

    @property
    def is_boolean(self) -> bool:
        return self.width == 1 and self.signedness is FormalSignedness.BIT

    def render(self) -> str:
        """Return a deterministic backend-independent report spelling."""
        raise NotImplementedError

    def observations(self) -> tuple["ObservationRef", ...]:
        """Return distinct observation leaves in deterministic first-use order."""
        seen: set[ObservationRef] = set()
        result: list[ObservationRef] = []
        for observation in iter_observations(self):
            if observation not in seen:
                seen.add(observation)
                result.append(observation)
        return tuple(result)

    def observation_ids(self) -> tuple[str, ...]:
        """Return distinct binding IDs, ignoring current/previous sampling."""
        return tuple(dict.fromkeys(item.semantic_signal_id for item in self.observations()))

    def to_data(self) -> dict[str, object]:
        """Return the strict, versioned, JSON-compatible representation."""
        return formal_predicate_to_data(self)

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> Self:
        """Restore a value and require the requested concrete node type."""
        value = formal_predicate_from_data(data)
        if cls is not FormalPredicate and not isinstance(value, cls):
            raise FormalPredicateError(
                f"formal predicate contains {type(value).__name__}, expected {cls.__name__}"
            )
        return value  # type: ignore[return-value]


def _validate_type(width: int, signedness: FormalSignedness) -> None:
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise FormalPredicateError("formal value width must be a positive integer")
    if not isinstance(signedness, FormalSignedness):
        raise FormalPredicateError("formal value signedness must be FormalSignedness")
    if signedness is FormalSignedness.BIT and width != 1:
        raise FormalPredicateError("bit formal values must have width one")


def _require_value(value: object, description: str) -> "FormalPredicate":
    if not isinstance(value, FormalPredicate):
        raise FormalPredicateError(f"{description} must be a formal predicate value")
    return value


@dataclass(frozen=True)
class ObservationRef(FormalPredicate):
    semantic_signal_id: str
    width: int
    signedness: FormalSignedness
    cycle: ObservationCycle = ObservationCycle.CURRENT

    def __post_init__(self) -> None:
        if not isinstance(self.semantic_signal_id, str) or not self.semantic_signal_id:
            raise FormalPredicateError("formal observation requires a semantic signal ID")
        if self.semantic_signal_id.strip() != self.semantic_signal_id:
            raise FormalPredicateError("formal observation ID must not contain surrounding whitespace")
        _validate_type(self.width, self.signedness)
        if not isinstance(self.cycle, ObservationCycle):
            raise FormalPredicateError("formal observation cycle must be ObservationCycle")

    def render(self) -> str:
        if self.cycle is ObservationCycle.CURRENT:
            return self.semantic_signal_id
        return f"previous({self.semantic_signal_id})"


@dataclass(frozen=True)
class Constant(FormalPredicate):
    value: int
    width: int
    signedness: FormalSignedness

    def __post_init__(self) -> None:
        _validate_type(self.width, self.signedness)
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise FormalPredicateError("formal constant value must be an integer")
        if self.signedness is FormalSignedness.SIGNED:
            minimum = -(1 << (self.width - 1))
            maximum = (1 << (self.width - 1)) - 1
        else:
            minimum = 0
            maximum = (1 << self.width) - 1
        if not minimum <= self.value <= maximum:
            raise FormalPredicateError(
                f"formal constant {self.value} does not fit "
                f"{self.signedness.value} width {self.width}"
            )

    def render(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class Unary(FormalPredicate):
    operator: FormalUnaryOperator
    operand: FormalPredicate
    width: int
    signedness: FormalSignedness

    def __post_init__(self) -> None:
        if not isinstance(self.operator, FormalUnaryOperator):
            raise FormalPredicateError("formal unary operator must be FormalUnaryOperator")
        operand = _require_value(self.operand, "formal unary operand")
        _validate_type(self.width, self.signedness)
        if self.operator is FormalUnaryOperator.LOGICAL_NOT:
            if not operand.is_boolean or not self.is_boolean:
                raise FormalPredicateError("logical_not requires and returns a bit predicate")
        elif self.operator is FormalUnaryOperator.BITWISE_NOT:
            if (self.width, self.signedness) != (operand.width, operand.signedness):
                raise FormalPredicateError("bitwise_not must preserve width and signedness")
        elif self.operator is FormalUnaryOperator.RESIZE:
            # Resize is explicit.  Its source and destination shapes are fully
            # recorded, so extension/truncation is never inferred from text.
            pass

    def render(self) -> str:
        operand = self.operand.render()
        if self.operator is FormalUnaryOperator.LOGICAL_NOT:
            return f"!({operand})"
        if self.operator is FormalUnaryOperator.BITWISE_NOT:
            return f"~({operand})"
        return f"resize<{self.width},{self.signedness.value}>({operand})"


_LOGICAL = {
    FormalBinaryOperator.LOGICAL_AND,
    FormalBinaryOperator.LOGICAL_OR,
    FormalBinaryOperator.IMPLIES,
}
_ARITHMETIC = {
    FormalBinaryOperator.ADD,
    FormalBinaryOperator.SUBTRACT,
    FormalBinaryOperator.MULTIPLY,
}
_BITWISE = {
    FormalBinaryOperator.BIT_AND,
    FormalBinaryOperator.BIT_OR,
    FormalBinaryOperator.BIT_XOR,
}
_COMPARISON = {
    FormalBinaryOperator.EQUAL,
    FormalBinaryOperator.NOT_EQUAL,
    FormalBinaryOperator.LESS,
    FormalBinaryOperator.LESS_EQUAL,
    FormalBinaryOperator.GREATER,
    FormalBinaryOperator.GREATER_EQUAL,
}
_ORDERED_COMPARISON = {
    FormalBinaryOperator.LESS,
    FormalBinaryOperator.LESS_EQUAL,
    FormalBinaryOperator.GREATER,
    FormalBinaryOperator.GREATER_EQUAL,
}


@dataclass(frozen=True)
class Binary(FormalPredicate):
    operator: FormalBinaryOperator
    left: FormalPredicate
    right: FormalPredicate
    width: int
    signedness: FormalSignedness

    def __post_init__(self) -> None:
        if not isinstance(self.operator, FormalBinaryOperator):
            raise FormalPredicateError("formal binary operator must be FormalBinaryOperator")
        left = _require_value(self.left, "formal binary left operand")
        right = _require_value(self.right, "formal binary right operand")
        _validate_type(self.width, self.signedness)
        output = (self.width, self.signedness)
        left_type = (left.width, left.signedness)
        right_type = (right.width, right.signedness)

        if self.operator in _LOGICAL:
            if not left.is_boolean or not right.is_boolean or not self.is_boolean:
                raise FormalPredicateError(
                    f"{self.operator.value} requires and returns bit predicates"
                )
            return
        if self.operator in _COMPARISON:
            if left_type != right_type:
                raise FormalPredicateError(
                    f"{self.operator.value} operands must have identical width and signedness"
                )
            if not self.is_boolean:
                raise FormalPredicateError(f"{self.operator.value} must return a bit predicate")
            if (
                self.operator in _ORDERED_COMPARISON
                and left.signedness not in {FormalSignedness.UNSIGNED, FormalSignedness.SIGNED}
            ):
                raise FormalPredicateError(
                    f"{self.operator.value} requires an unsigned or signed numeric operand"
                )
            return
        if self.operator in _BITWISE:
            if left_type != right_type or output != left_type:
                raise FormalPredicateError(
                    f"{self.operator.value} requires equal operands and preserves their type"
                )
            return
        if self.operator in _ARITHMETIC:
            if left.signedness not in {FormalSignedness.UNSIGNED, FormalSignedness.SIGNED}:
                raise FormalPredicateError(
                    f"{self.operator.value} requires numeric operands"
                )
            if left.signedness is not right.signedness or self.signedness is not left.signedness:
                raise FormalPredicateError(
                    f"{self.operator.value} operands and result must have matching signedness"
                )
            if self.width < max(left.width, right.width):
                raise FormalPredicateError(
                    f"{self.operator.value} result cannot be narrower than either operand"
                )
            return
        if self.operator in {
            FormalBinaryOperator.SHIFT_LEFT,
            FormalBinaryOperator.SHIFT_RIGHT,
        }:
            if output != left_type:
                raise FormalPredicateError(
                    f"{self.operator.value} must preserve the left operand type"
                )
            if right.signedness not in {
                FormalSignedness.BIT,
                FormalSignedness.UNSIGNED,
                FormalSignedness.BITS,
            }:
                raise FormalPredicateError(
                    f"{self.operator.value} amount must be unsigned"
                )
            return
        raise FormalPredicateError(f"unsupported formal binary operator: {self.operator.value}")

    def render(self) -> str:
        symbol = {
            FormalBinaryOperator.LOGICAL_AND: "&&",
            FormalBinaryOperator.LOGICAL_OR: "||",
            FormalBinaryOperator.IMPLIES: "->",
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
        }[self.operator]
        return f"({self.left.render()} {symbol} {self.right.render()})"


@dataclass(frozen=True)
class Mux(FormalPredicate):
    condition: FormalPredicate
    when_true: FormalPredicate
    when_false: FormalPredicate
    width: int
    signedness: FormalSignedness

    def __post_init__(self) -> None:
        condition = _require_value(self.condition, "formal mux condition")
        when_true = _require_value(self.when_true, "formal mux true branch")
        when_false = _require_value(self.when_false, "formal mux false branch")
        _validate_type(self.width, self.signedness)
        if not condition.is_boolean:
            raise FormalPredicateError("formal mux condition must be a bit predicate")
        shape = (self.width, self.signedness)
        if shape != (when_true.width, when_true.signedness) or shape != (
            when_false.width,
            when_false.signedness,
        ):
            raise FormalPredicateError("formal mux branches and result must have identical types")

    def render(self) -> str:
        return (
            f"({self.condition.render()} ? {self.when_true.render()} : "
            f"{self.when_false.render()})"
        )


def require_predicate(value: FormalPredicate) -> FormalPredicate:
    """Require a one-bit boolean property root and return it unchanged."""
    value = _require_value(value, "formal property root")
    if not value.is_boolean:
        raise FormalPredicateError("formal property root must be a bit predicate")
    return value


def iter_observations(value: FormalPredicate) -> Iterator[ObservationRef]:
    """Yield every observation leaf in stable depth-first operand order."""
    value = _require_value(value, "formal observation traversal root")
    if isinstance(value, ObservationRef):
        yield value
    elif isinstance(value, Constant):
        return
    elif isinstance(value, Unary):
        yield from iter_observations(value.operand)
    elif isinstance(value, Binary):
        yield from iter_observations(value.left)
        yield from iter_observations(value.right)
    elif isinstance(value, Mux):
        yield from iter_observations(value.condition)
        yield from iter_observations(value.when_true)
        yield from iter_observations(value.when_false)
    else:
        raise FormalPredicateError(f"unsupported formal predicate node: {type(value).__name__}")


def map_observations(
    value: FormalPredicate,
    mapper: Callable[[ObservationRef], ObservationRef],
) -> FormalPredicate:
    """Rebuild a predicate after mapping every observation leaf exactly once."""

    value = _require_value(value, "formal observation mapping root")
    if isinstance(value, ObservationRef):
        mapped = mapper(value)
        if not isinstance(mapped, ObservationRef):
            raise FormalPredicateError("formal observation mapper must return ObservationRef")
        return mapped
    if isinstance(value, Constant):
        return value
    if isinstance(value, Unary):
        return Unary(
            value.operator, map_observations(value.operand, mapper),
            value.width, value.signedness,
        )
    if isinstance(value, Binary):
        return Binary(
            value.operator,
            map_observations(value.left, mapper),
            map_observations(value.right, mapper),
            value.width,
            value.signedness,
        )
    if isinstance(value, Mux):
        return Mux(
            map_observations(value.condition, mapper),
            map_observations(value.when_true, mapper),
            map_observations(value.when_false, mapper),
            value.width,
            value.signedness,
        )
    raise FormalPredicateError(f"unsupported formal predicate node: {type(value).__name__}")


def _node_to_data(value: FormalPredicate) -> dict[str, object]:
    common: dict[str, object] = {
        "width": value.width,
        "signedness": value.signedness.value,
    }
    if isinstance(value, ObservationRef):
        return {
            "tag": "observation",
            "semantic_signal_id": value.semantic_signal_id,
            "cycle": value.cycle.value,
            **common,
        }
    if isinstance(value, Constant):
        return {"tag": "constant", "value": value.value, **common}
    if isinstance(value, Unary):
        return {
            "tag": "unary",
            "operator": value.operator.value,
            "operand": _node_to_data(value.operand),
            **common,
        }
    if isinstance(value, Binary):
        return {
            "tag": "binary",
            "operator": value.operator.value,
            "left": _node_to_data(value.left),
            "right": _node_to_data(value.right),
            **common,
        }
    if isinstance(value, Mux):
        return {
            "tag": "mux",
            "condition": _node_to_data(value.condition),
            "when_true": _node_to_data(value.when_true),
            "when_false": _node_to_data(value.when_false),
            **common,
        }
    raise FormalPredicateError(f"unsupported formal predicate node: {type(value).__name__}")


def formal_predicate_to_data(value: FormalPredicate) -> dict[str, object]:
    value = _require_value(value, "formal serialization root")
    return {"schema": FORMAL_PREDICATE_SCHEMA, "node": _node_to_data(value)}


def _exact_keys(data: Mapping[str, object], expected: set[str], description: str) -> None:
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise FormalPredicateError(f"{description} has invalid fields: {'; '.join(details)}")


def _enum(enum_type: type[Enum], value: object, description: str) -> Enum:
    if not isinstance(value, str):
        raise FormalPredicateError(f"{description} must be a string")
    try:
        return enum_type(value)
    except ValueError as error:
        raise FormalPredicateError(f"unsupported {description}: {value}") from error


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FormalPredicateError(f"{description} must be an integer")
    return value


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FormalPredicateError(f"{description} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise FormalPredicateError(f"{description} keys must be strings")
    return value


def _node_from_data(value: object) -> FormalPredicate:
    data = _mapping(value, "formal predicate node")
    tag = data.get("tag")
    if not isinstance(tag, str):
        raise FormalPredicateError("formal predicate node tag must be a string")
    common = {"tag", "width", "signedness"}
    signedness = _enum(
        FormalSignedness, data.get("signedness"), "formal signedness"
    )
    assert isinstance(signedness, FormalSignedness)
    width = _integer(data.get("width"), "formal width")

    if tag == "observation":
        _exact_keys(data, common | {"semantic_signal_id", "cycle"}, "observation node")
        semantic_id = data["semantic_signal_id"]
        if not isinstance(semantic_id, str):
            raise FormalPredicateError("formal observation ID must be a string")
        cycle = _enum(ObservationCycle, data["cycle"], "formal observation cycle")
        assert isinstance(cycle, ObservationCycle)
        return ObservationRef(semantic_id, width, signedness, cycle)
    if tag == "constant":
        _exact_keys(data, common | {"value"}, "constant node")
        return Constant(_integer(data["value"], "formal constant value"), width, signedness)
    if tag == "unary":
        _exact_keys(data, common | {"operator", "operand"}, "unary node")
        operator = _enum(FormalUnaryOperator, data["operator"], "formal unary operator")
        assert isinstance(operator, FormalUnaryOperator)
        return Unary(operator, _node_from_data(data["operand"]), width, signedness)
    if tag == "binary":
        _exact_keys(data, common | {"operator", "left", "right"}, "binary node")
        operator = _enum(FormalBinaryOperator, data["operator"], "formal binary operator")
        assert isinstance(operator, FormalBinaryOperator)
        return Binary(
            operator,
            _node_from_data(data["left"]),
            _node_from_data(data["right"]),
            width,
            signedness,
        )
    if tag == "mux":
        _exact_keys(
            data,
            common | {"condition", "when_true", "when_false"},
            "mux node",
        )
        return Mux(
            _node_from_data(data["condition"]),
            _node_from_data(data["when_true"]),
            _node_from_data(data["when_false"]),
            width,
            signedness,
        )
    raise FormalPredicateError(f"unsupported formal predicate node tag: {tag}")


def formal_predicate_from_data(data: Mapping[str, object]) -> FormalPredicate:
    envelope = _mapping(data, "formal predicate serialization")
    _exact_keys(envelope, {"schema", "node"}, "formal predicate serialization")
    if envelope["schema"] != FORMAL_PREDICATE_SCHEMA:
        raise FormalPredicateError(
            f"unsupported formal predicate schema: {envelope['schema']}"
        )
    return _node_from_data(envelope["node"])


__all__ = [
    "Binary",
    "Constant",
    "FORMAL_PREDICATE_SCHEMA",
    "FormalBinaryOperator",
    "FormalPredicate",
    "FormalPredicateError",
    "FormalSignedness",
    "FormalUnaryOperator",
    "Mux",
    "ObservationCycle",
    "ObservationRef",
    "formal_predicate_from_data",
    "formal_predicate_to_data",
    "iter_observations",
    "map_observations",
    "require_predicate",
]
