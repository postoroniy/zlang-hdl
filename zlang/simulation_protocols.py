# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Stable protocol-simulation lowering API with one owner per protocol."""

from zlang.ir.interfaces import ready_valid_field_name
from zlang.simulation_credit import lower_credit_module
from zlang.simulation_packet import lower_packet_arbiter_module
from zlang.simulation_protocol_adapters import lower_adapter_module
from zlang.simulation_protocol_hierarchy import (
    lower_aggregate_protocol_hierarchy,
    lower_credit_hierarchy,
    lower_protocol_hierarchy,
    lower_ready_valid_hierarchy,
    lower_request_response_hierarchy,
)
from zlang.simulation_protocol_shared import (
    ProtocolSimulationLoweringError,
    RUNTIME_PROTOCOL_SCOPE_PREFIX,
    credit_field_name,
    credit_state_name,
    packet_field_name,
    packet_state_name,
    request_response_field_name,
    request_response_state_name,
    vc_credit_field_name,
    vc_credit_state_name,
)
from zlang.simulation_protocol_pipeline import lower_protocol_module
from zlang.simulation_ready_valid import lower_ready_valid_module
from zlang.simulation_request_response import lower_request_response_module
from zlang.simulation_vc_credit import lower_vc_credit_module

__all__ = [
    "ProtocolSimulationLoweringError",
    "RUNTIME_PROTOCOL_SCOPE_PREFIX",
    "credit_field_name",
    "credit_state_name",
    "lower_adapter_module",
    "lower_credit_module",
    "lower_credit_hierarchy",
    "lower_aggregate_protocol_hierarchy",
    "lower_request_response_module",
    "lower_request_response_hierarchy",
    "lower_packet_arbiter_module",
    "lower_protocol_hierarchy",
    "lower_protocol_module",
    "lower_ready_valid_module",
    "lower_ready_valid_hierarchy",
    "lower_vc_credit_module",
    "packet_field_name",
    "packet_state_name",
    "request_response_field_name",
    "request_response_state_name",
    "ready_valid_field_name",
    "vc_credit_field_name",
    "vc_credit_state_name",
]
