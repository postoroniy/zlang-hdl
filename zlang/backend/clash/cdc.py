"""Backend-local Clash lowering for explicit CDC crossings."""

from __future__ import annotations

from zlang.backend.clash.ports import forward_port_annotation

from dataclasses import dataclass
import re
from typing import Callable, Iterable

from zlang.ir.cdc import CrossingKind
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module
from zlang.ir.types import HardwareType, StructType


@dataclass(frozen=True)
class CDCRendering:
    """The established Clash rendering operations required by CDC lowering."""

    error: Callable[[str], Exception]
    emit_struct: Callable[[StructType], str]
    all_structs: Callable[[Module], Iterable[StructType]]
    ready_valid_declarations: Callable[[], str]
    emit_type: Callable[[HardwareType], str]
    zero_value: Callable[[HardwareType], str]


def domain_type(clock_name: str) -> str:
    words = [word for word in re.split(r"_+", clock_name) if word]
    rendered = "".join(word[:1].upper() + word[1:] for word in words)
    return rendered + "Domain"


def emit_cdc_module(module: Module, rendering: CDCRendering) -> str:
    """Emit one explicit crossing between two named clock domains."""

    crossings = [
        connection
        for connection in module.connections
        if connection.crossing is not None
    ]
    if len(crossings) != 1 or len(module.connections) != 1:
        raise rendering.error(
            "CDC backend currently requires exactly one explicit crossing"
        )
    if module.assignments or module.registers or module.rules:
        raise rendering.error(
            "CDC modules with explicit assignments, registers, or rules are not implemented"
        )
    if module.fifos or module.memories or module.csr_blocks:
        raise rendering.error(
            "CDC modules cannot yet be mixed with storage or CSR resources"
        )
    connection = crossings[0]
    crossing = connection.crossing
    assert crossing is not None
    source = connection.source
    destination = connection.destination
    if source.domain is None or destination.domain is None:
        raise rendering.error("CDC endpoints require named clock domains")
    domains = {domain.clock: domain for domain in module.clock_domains}
    source_domain = domains[source.domain]
    destination_domain = domains[destination.domain]
    source_type = domain_type(source_domain.clock)
    destination_type = domain_type(destination_domain.clock)
    if source_type == destination_type:
        raise rendering.error("clock domain names collide after Clash lowering")

    extensions = [
        "{-# LANGUAGE DataKinds #-}",
        "{-# LANGUAGE TemplateHaskell #-}",
    ]
    declarations = ""
    imports = (
        "import Clash.Explicit.Prelude\n"
        "import qualified Clash.Explicit.Signal as Explicit\n"
        "import qualified Clash.Explicit.Synchronizer as Synchronizer\n"
    )
    if source.protocol is InterfaceProtocol.READY_VALID:
        extensions.extend(
            ("{-# LANGUAGE DeriveAnyClass #-}", "{-# LANGUAGE DeriveGeneric #-}")
        )
        imports += "import GHC.Generics (Generic)\n"
        declarations = "".join(
            (
                *(
                    f"{rendering.emit_struct(type_)}\n"
                    for type_ in rendering.all_structs(module)
                ),
                rendering.ready_valid_declarations(),
            )
        )
    if crossing.kind is CrossingKind.ASYNC_FIFO:
        extensions.append("{-# LANGUAGE TypeApplications #-}")
    extensions.append("{-# LANGUAGE NoImplicitPrelude #-}")
    extension_block = "\n".join(extensions) + "\n"
    domain_declarations = "\n".join(
        f'createDomain vSystem{{vName="{domain_type(domain.clock)}", '
        'vResetKind=Synchronous}'
        for domain in module.clock_domains
    )
    timing_signature = [
        item
        for domain in module.clock_domains
        for item in (
            f"Clock {domain_type(domain.clock)}",
            f"Reset {domain_type(domain.clock)}",
        )
    ]
    timing_arguments = [
        item
        for domain in module.clock_domains
        for item in (domain.clock, domain.reset)
    ]
    timing_annotations = [f'PortName "{item}"' for item in timing_arguments]

    if crossing.kind in {CrossingKind.SYNC_LEVEL, CrossingKind.PULSE_TOGGLE}:
        input_type = f"Signal {source_type} (Bit)"
        output_type = f"Signal {destination_type} (Bit)"
        signature = " -> ".join((*timing_signature, input_type, output_type))
        arguments = " ".join((*timing_arguments, source.name))
        annotations = ", ".join(
            (*timing_annotations, f'PortName "{source.name}"')
        )
        if crossing.kind is CrossingKind.SYNC_LEVEL:
            equation = (
                f"topEntity {arguments} = "
                f"Synchronizer.dualFlipFlopSynchronizer "
                f"{source_domain.clock} {destination_domain.clock} "
                f"{destination_domain.reset} enableGen low {source.name}"
            )
        else:
            equation = f'''topEntity {arguments} = {destination.name}
 where
  source_toggle = register {source_domain.clock} {source_domain.reset} enableGen low source_toggle_next
  source_toggle_next = (\\toggle pulse -> if pulse == high then if toggle == high then low else high else toggle) <$> source_toggle <*> {source.name}
  synchronized_toggle = Synchronizer.dualFlipFlopSynchronizer {source_domain.clock} {destination_domain.clock} {destination_domain.reset} enableGen low source_toggle
  previous_toggle = register {destination_domain.clock} {destination_domain.reset} enableGen low synchronized_toggle
  {destination.name} = xor <$> synchronized_toggle <*> previous_toggle'''
        return f'''{extension_block}
module {module.name} where

{imports}
{domain_declarations}

{equation.splitlines()[0].replace(f'topEntity {arguments}', f'topEntity :: {signature}\ntopEntity {arguments}', 1)}{''.join(chr(10) + line for line in equation.splitlines()[1:])}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{annotations}]
    , t_output = PortName "{destination.name}"
    }}) #-}}
'''

    payload_type = rendering.emit_type(source.type)
    forward_source = (
        f"Signal {source_type} (ZLangReadyValidForward ({payload_type}))"
    )
    backward_destination = f"Signal {destination_type} ZLangReadyValidBackward"
    output_type = (
        f"(Signal {source_type} ZLangReadyValidBackward, "
        f"Signal {destination_type} (ZLangReadyValidForward ({payload_type})))"
    )
    signature = " -> ".join(
        (*timing_signature, forward_source, backward_destination, output_type)
    )
    source_input = source.name
    destination_backward = f"{destination.name}_backward"
    arguments = " ".join((*timing_arguments, source_input, destination_backward))
    annotations = ", ".join(
        (
            *timing_annotations,
            forward_port_annotation(source.name, source.type, "valid"),
            f'PortName "{destination.name}_ready"',
        )
    )
    output_annotation = (
        f'PortProduct "" [PortName "{source.name}_ready", '
        f'{forward_port_annotation(destination.name, destination.type, "valid")}]'
    )
    common = f'''  source_payload = zlangRvPayload <$> {source_input}
  source_valid = zlangRvValid <$> {source_input}
  destination_ready = zlangRvReady <$> {destination_backward}
  source_reset_active = unsafeToActiveHigh {source_domain.reset}
  destination_reset_active = unsafeToActiveHigh {destination_domain.reset}'''

    if crossing.kind is CrossingKind.HANDSHAKE:
        zero = rendering.zero_value(source.type)
        bindings = f'''{common}
  source_ready = (\\request acknowledge resetActive -> if resetActive || request /= acknowledge then low else high) <$> source_request <*> synchronized_acknowledge <*> source_reset_active
  source_transfer = (\\valid ready -> valid .&. ready) <$> source_valid <*> source_ready
  source_data = register {source_domain.clock} {source_domain.reset} enableGen {zero} source_data_next
  source_data_next = (\\held payload transferred -> if transferred == high then payload else held) <$> source_data <*> source_payload <*> source_transfer
  source_request = register {source_domain.clock} {source_domain.reset} enableGen low source_request_next
  source_request_next = (\\request transferred -> if transferred == high then if request == high then low else high else request) <$> source_request <*> source_transfer
  synchronized_acknowledge = Synchronizer.dualFlipFlopSynchronizer {destination_domain.clock} {source_domain.clock} {source_domain.reset} enableGen low destination_acknowledge
  synchronized_request = Synchronizer.dualFlipFlopSynchronizer {source_domain.clock} {destination_domain.clock} {destination_domain.reset} enableGen low source_request
  crossed_data = Explicit.unsafeSynchronizer {source_domain.clock} {destination_domain.clock} source_data
  destination_valid = (\\request acknowledge resetActive -> if resetActive || request == acknowledge then low else high) <$> synchronized_request <*> destination_acknowledge <*> destination_reset_active
  destination_payload = crossed_data
  destination_transfer = (\\valid ready -> valid .&. ready) <$> destination_valid <*> destination_ready
  destination_acknowledge = register {destination_domain.clock} {destination_domain.reset} enableGen low destination_acknowledge_next
  destination_acknowledge_next = (\\acknowledge request transferred -> if transferred == high then request else acknowledge) <$> destination_acknowledge <*> synchronized_request <*> destination_transfer'''
    else:
        assert crossing.depth is not None
        address_size = crossing.depth.bit_length() - 1
        bindings = f'''{common}
  (destination_payload, destination_empty, source_full) = Synchronizer.asyncFIFOSynchronizer (SNat @{address_size}) {source_domain.clock} {destination_domain.clock} {source_domain.reset} {destination_domain.reset} enableGen enableGen destination_read source_write
  source_ready = (\\full resetActive -> if resetActive || full then low else high) <$> source_full <*> source_reset_active
  source_write = (\\payload valid ready -> if valid == high && ready == high then Just payload else Nothing) <$> source_payload <*> source_valid <*> source_ready
  destination_valid = (\\empty resetActive -> if resetActive || empty then low else high) <$> destination_empty <*> destination_reset_active
  destination_read = (\\valid ready -> valid == high && ready == high) <$> destination_valid <*> destination_ready'''

    return f'''{extension_block}
module {module.name} where

{imports}
{declarations}{domain_declarations}

topEntity :: {signature}
topEntity {arguments} = (ZLangReadyValidBackward <$> source_ready, ZLangReadyValidForward <$> destination_payload <*> destination_valid)
 where
{bindings}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{annotations}]
    , t_output = {output_annotation}
    }}) #-}}
'''


__all__ = ["CDCRendering", "domain_type", "emit_cdc_module"]
