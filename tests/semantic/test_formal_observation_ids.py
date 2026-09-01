from pathlib import Path

import pytest

from zlang.formal import build_recursive_formal_design
from zlang.ir.formal_observations import (
    FormalObservationIdError,
    RequestResponseObservationSignal,
    request_response_observation_id,
)
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]


def test_request_response_observation_appends_to_qualified_connection_once():
    connection = "rr:Top:requester.bus->responder.bus"

    assert request_response_observation_id(
        connection, RequestResponseObservationSignal.OUTSTANDING
    ) == "rr:Top:requester.bus->responder.bus:outstanding"

    with pytest.raises(
        FormalObservationIdError, match="must be rr:-qualified"
    ):
        request_response_observation_id(
            "Top:requester.bus->responder.bus", "outstanding"
        )


def test_recursive_request_response_ids_match_the_connection_identity():
    module = analyze(parse(
        (ROOT / "examples/hierarchical_request_response_m40.zl").read_text()
    ))
    connection = module.request_response_connections[0]
    design = build_recursive_formal_design(module)
    actual = {
        item.ref.local_semantic_id
        for item in design.bindings
        if item.ref.local_semantic_id.startswith("rr:")
    }
    expected = {
        request_response_observation_id(connection.semantic_id, signal)
        for signal in RequestResponseObservationSignal
    }

    assert actual == expected
    assert all(not item.startswith("rr:rr:") for item in actual)
