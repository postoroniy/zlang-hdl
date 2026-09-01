from dataclasses import FrozenInstanceError

import pytest

from zlang.ir.formal_predicates import (
    Binary,
    Constant,
    FORMAL_PREDICATE_SCHEMA,
    FormalBinaryOperator,
    FormalPredicate,
    FormalPredicateError,
    FormalSignedness,
    FormalUnaryOperator,
    Mux,
    ObservationCycle,
    ObservationRef,
    Unary,
    formal_predicate_from_data,
    iter_observations,
    require_predicate,
)


BIT = FormalSignedness.BIT
UNSIGNED = FormalSignedness.UNSIGNED


def bit(name: str, cycle: ObservationCycle = ObservationCycle.CURRENT) -> ObservationRef:
    return ObservationRef(name, 1, BIT, cycle)


def test_render_and_observation_traversal_are_deterministic() -> None:
    valid_now = bit("port:tx.valid")
    ready_before = bit("port:tx.ready", ObservationCycle.PREVIOUS)
    valid_before = bit("port:tx.valid", ObservationCycle.PREVIOUS)
    stalled_before = Binary(
        FormalBinaryOperator.LOGICAL_AND,
        valid_before,
        Unary(FormalUnaryOperator.LOGICAL_NOT, ready_before, 1, BIT),
        1,
        BIT,
    )
    predicate = Binary(
        FormalBinaryOperator.IMPLIES,
        stalled_before,
        valid_now,
        1,
        BIT,
    )

    assert predicate.render() == (
        "((previous(port:tx.valid) && !(previous(port:tx.ready))) "
        "-> port:tx.valid)"
    )
    assert tuple(iter_observations(predicate)) == (
        valid_before,
        ready_before,
        valid_now,
    )
    assert predicate.observations() == (valid_before, ready_before, valid_now)
    assert predicate.observation_ids() == ("port:tx.valid", "port:tx.ready")
    assert require_predicate(predicate) is predicate


def test_typed_arithmetic_comparison_mux_and_resize_round_trip() -> None:
    count = ObservationRef("fifo:q.count", 3, UNSIGNED)
    previous = ObservationRef(
        "fifo:q.count", 3, UNSIGNED, ObservationCycle.PREVIOUS
    )
    incremented = Binary(
        FormalBinaryOperator.ADD,
        previous,
        Constant(1, 1, UNSIGNED),
        4,
        UNSIGNED,
    )
    selected = Mux(
        bit("fifo:q.push", ObservationCycle.PREVIOUS),
        incremented,
        Unary(FormalUnaryOperator.RESIZE, previous, 4, UNSIGNED),
        4,
        UNSIGNED,
    )
    predicate = Binary(
        FormalBinaryOperator.EQUAL,
        Unary(FormalUnaryOperator.RESIZE, count, 4, UNSIGNED),
        selected,
        1,
        BIT,
    )

    data = predicate.to_data()
    assert data["schema"] == FORMAL_PREDICATE_SCHEMA
    assert FormalPredicate.from_data(data) == predicate
    assert formal_predicate_from_data(data) == predicate
    assert predicate.to_data() == FormalPredicate.from_data(data).to_data()
    assert predicate.render() == (
        "(resize<4,unsigned>(fifo:q.count) == "
        "(previous(fifo:q.push) ? (previous(fifo:q.count) + 1) : "
        "resize<4,unsigned>(previous(fifo:q.count))))"
    )


def test_nodes_are_immutable() -> None:
    observation = bit("port:x")
    with pytest.raises(FrozenInstanceError):
        observation.width = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory,diagnostic",
    (
        (lambda: ObservationRef("x", 0, UNSIGNED), "positive integer"),
        (lambda: ObservationRef(" x", 1, BIT), "surrounding whitespace"),
        (lambda: ObservationRef("x", 2, BIT), "width one"),
        (lambda: ObservationRef("x", 1, "bit"), "FormalSignedness"),
        (lambda: ObservationRef("x", 1, BIT, "current"), "ObservationCycle"),
        (lambda: Constant(2, 1, BIT), "does not fit"),
        (lambda: Constant(-1, 2, UNSIGNED), "does not fit"),
        (lambda: Constant(True, 1, BIT), "must be an integer"),
    ),
)
def test_leaf_validation_is_strict(factory, diagnostic) -> None:
    with pytest.raises(FormalPredicateError, match=diagnostic):
        factory()


def test_operator_width_signedness_and_arity_are_strict() -> None:
    a = bit("a")
    u2 = ObservationRef("u2", 2, UNSIGNED)
    signed2 = ObservationRef("s2", 2, FormalSignedness.SIGNED)

    invalid = (
        lambda: Unary(FormalUnaryOperator.LOGICAL_NOT, u2, 1, BIT),
        lambda: Unary(FormalUnaryOperator.BITWISE_NOT, u2, 3, UNSIGNED),
        lambda: Binary(FormalBinaryOperator.LOGICAL_AND, a, u2, 1, BIT),
        lambda: Binary(FormalBinaryOperator.EQUAL, u2, signed2, 1, BIT),
        lambda: Binary(FormalBinaryOperator.BIT_AND, u2, u2, 3, UNSIGNED),
        lambda: Binary(FormalBinaryOperator.ADD, u2, signed2, 3, UNSIGNED),
        lambda: Binary(FormalBinaryOperator.SHIFT_LEFT, u2, signed2, 2, UNSIGNED),
        lambda: Mux(u2, u2, u2, 2, UNSIGNED),
        lambda: Mux(a, u2, signed2, 2, UNSIGNED),
    )
    for factory in invalid:
        with pytest.raises(FormalPredicateError):
            factory()

    with pytest.raises(FormalPredicateError, match="property root"):
        require_predicate(u2)


@pytest.mark.parametrize(
    "mutate,diagnostic",
    (
        (lambda data: data.update(schema="future"), "unsupported formal predicate schema"),
        (lambda data: data.update(extra=True), "unexpected extra"),
        (lambda data: data["node"].update(tag="future"), "unsupported formal predicate node"),
        (lambda data: data["node"].update(extra=True), "unexpected extra"),
        (lambda data: data["node"].pop("right"), "missing right"),
        (lambda data: data["node"].update(operator="eventually"), "unsupported formal binary"),
        (lambda data: data["node"]["left"].update(cycle="next"), "unsupported formal observation cycle"),
    ),
)
def test_deserialization_rejects_unknown_schema_tags_fields_and_temporal_forms(
    mutate, diagnostic
) -> None:
    value = Binary(
        FormalBinaryOperator.EQUAL,
        bit("a"),
        bit("b", ObservationCycle.PREVIOUS),
        1,
        BIT,
    )
    data = value.to_data()
    mutate(data)
    with pytest.raises(FormalPredicateError, match=diagnostic):
        FormalPredicate.from_data(data)


def test_concrete_from_data_rejects_a_different_node_tag() -> None:
    with pytest.raises(FormalPredicateError, match="expected ObservationRef"):
        ObservationRef.from_data(Constant(0, 1, BIT).to_data())
