# SPDX-License-Identifier: Apache-2.0
"""Run-owned, bounded typed JSONL channels; no hardware execution semantics."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import os
import io
import stat

from zlang.ir.type_codec import canonical_type_data
from zlang.simulation_logic import LogicVector
from zlang.simulation_plan_policy import MAX_PLAN_BYTES
from zlang.simulation_values import SimulationRuntimeError, pack_value, unpack_value


def decode_field(value, type_):
    if isinstance(value, dict) and set(value) == {"bits"}:
        bits = value["bits"]
        if (
            not isinstance(bits, str)
            or len(bits) != type_.width
            or any(c not in "01ux" for c in bits)
        ):
            raise SimulationRuntimeError(
                f"simulation value requires exactly {type_.width} packed 0/1/u/x bits"
            )
        parsed = LogicVector.parse(bits, type_.width)
        if parsed.is_binary:
            unpack_value(type_, parsed.value)
        return parsed
    return LogicVector.known(pack_value(type_, value), type_.width)


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise SimulationRuntimeError(f"duplicate JSON field '{name}'")
        result[name] = value
    return result


def snapshot_input(path):
    """Freeze one regular file through one descriptor, within the byte ceiling."""
    path = Path(path)
    if str(path) == "-":
        raise SimulationRuntimeError(
            "simulation input must be a regular file; stdin is unsupported"
        )
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise SimulationRuntimeError("simulation input must be a regular file")
        data = source.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise SimulationRuntimeError(
            "simulation input file exceeds the native plan byte limit"
        )
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def packed_records(text, path, bindings):
    """The same strict input codec for offline RTL/debugger preparation."""
    with io.StringIO(text) as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, object_pairs_hook=_unique_object)
                if not isinstance(value, dict) or set(value) != {
                    name for name, _ in bindings
                }:
                    raise SimulationRuntimeError(
                        "record fields must exactly match the read declaration"
                    )
                yield tuple(
                    decode_field(value[name], target.type) for name, target in bindings
                )
            except (
                ValueError,
                TypeError,
                RecursionError,
                SimulationRuntimeError,
            ) as error:
                raise SimulationRuntimeError(f"{path}:{number}: {error}") from error


def read_packed_records(path, bindings):
    text, _ = snapshot_input(path)
    yield from packed_records(text, path, bindings)


class SimulationChannels:
    """Own open handles, input cursor and completed-record counters for one run."""

    def __init__(self, product, inputs, outputs):
        self._stack = ExitStack()
        self._reader = None
        self._writers = {}
        self._metadata = []
        self._output_paths = {}
        self._line = 0
        self._position = None
        self._eof = False
        self.records = 0
        self.bytes_written = 0
        self.manifest = {
            "schema": "zlang-simulation-io-v1",
            "block": product.block.name,
            "clock": product.block.clock,
            "sample_phase": "pre_edge",
            "status": "running",
            "inputs": {},
            "outputs": {},
        }
        required_inputs = {name for name, _ in product.inputs}
        required_outputs = {name for name, _ in product.outputs}
        if set(inputs) != required_inputs or set(outputs) != required_outputs:
            raise SimulationRuntimeError(
                "simulation channel bindings must exactly match the selected block"
            )
        paths = [
            Path(p).resolve()
            for p in [*inputs.values(), *outputs.values()]
            if str(p) != "-"
        ]
        paths.extend(
            Path(str(p) + ".io.json").resolve()
            for p in outputs.values()
            if str(p) != "-"
        )
        if len(set(paths)) != len(paths):
            raise SimulationRuntimeError(
                "simulation channels require distinct input/output paths"
            )
        try:
            for name, bindings in product.inputs:
                path = Path(inputs[name])
                text, digest = snapshot_input(path)
                self._reader = self._stack.enter_context(io.StringIO(text))
                self._input_path = path
                self.manifest["inputs"][name] = {
                    "sha256": digest,
                    "snapshot": "launch",
                    "fields": {
                        key: canonical_type_data(value.type) for key, value in bindings
                    },
                }
            for name, bindings in product.outputs:
                path = outputs[name]
                if str(path) == "-":
                    writer = sys.stdout
                else:
                    path = Path(path)
                    writer = self._stack.enter_context(path.open("x", encoding="utf-8"))
                    metadata = self._stack.enter_context(
                        Path(str(path) + ".io.json").open("x", encoding="utf-8")
                    )
                    self._metadata.append(metadata)
                    self._output_paths[name] = path
                self._writers[name] = writer
                self.manifest["outputs"][name] = {
                    "fields": {
                        key: canonical_type_data(value.type) for key, value in bindings
                    },
                    "records": 0,
                }
        except BaseException:
            self._stack.close()
            raise

    def begin(self):
        if self._reader is not None:
            self._position = self._reader.tell(), self._line, self._eof

    def read(self, bindings):
        if self._eof:
            return None
        while True:
            line = self._reader.readline(MAX_PLAN_BYTES + 1)
            if len(line.encode("utf-8")) > MAX_PLAN_BYTES:
                raise SimulationRuntimeError(
                    "simulation JSONL record exceeds native plan byte limit"
                )
            if not line:
                self._eof = True
                return None
            self._line += 1
            if line.strip():
                break
        try:
            value = json.loads(line, object_pairs_hook=_unique_object)
            if not isinstance(value, dict) or set(value) != {
                name for name, _ in bindings
            }:
                raise SimulationRuntimeError(
                    "record fields must exactly match the read declaration"
                )
            return tuple(
                (target, decode_field(value[name], target.type))
                for name, target in bindings
            )
        except (ValueError, TypeError, RecursionError, SimulationRuntimeError) as error:
            raise SimulationRuntimeError(
                f"{self._input_path}:{self._line}: {error}"
            ) from error

    def rollback(self):
        if self._position is not None:
            offset, self._line, self._eof = self._position
            self._reader.seek(offset)
            self._position = None

    def stage(self, event, records):
        lines = []
        for channel, values in records:
            line = json.dumps(
                {"event": event, "epoch": 0, "values": values},
                sort_keys=True,
                separators=(",", ":"),
            )
            lines.append((channel, line + "\n"))
        if (
            self.bytes_written + sum(len(text.encode("utf-8")) for _, text in lines)
            > MAX_PLAN_BYTES
        ):
            raise SimulationRuntimeError(
                "simulation log exceeds native plan byte limit"
            )
        return lines

    def commit(self, lines):
        for channel, text in lines:
            self._writers[channel].write(text)
            self._writers[channel].flush()
            self.manifest["outputs"][channel]["records"] += 1
            self.bytes_written += len(text.encode("utf-8"))
        self._position = None
        self.records += 1

    def close(self, status, error=None):
        self.manifest["status"] = status
        self.manifest["completed_edges"] = self.records
        if error is not None:
            self.manifest["error"] = str(error)
        try:
            for name, path in self._output_paths.items():
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1 << 20), b""):
                        digest.update(chunk)
                self.manifest["outputs"][name]["sha256"] = digest.hexdigest()
            for destination in self._metadata:
                destination.write(
                    json.dumps(self.manifest, sort_keys=True, indent=2) + "\n"
                )
                destination.flush()
        finally:
            self._stack.close()
