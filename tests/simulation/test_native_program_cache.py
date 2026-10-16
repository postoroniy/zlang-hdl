# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Exact executable-plan reuse across the native simulation entry points."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

import pytest

from zlang import native_simulation, sim
from zlang.compiler import compile_file
from zlang.simulation_plan import JitUnsupportedFeatureError
from zlang.simulation_plan_build import build_simulation_plan
from zlang.simulation_state import SimulationStateSession


@pytest.fixture
def runtime(monkeypatch):
    native = sim._native_runtime()

    class CountingRuntime:
        calls = 0

        def compile_plan_bytes(self, payload):
            self.calls += 1
            time.sleep(0.001)
            return native.compile_plan_bytes(payload)

    runtime = CountingRuntime()
    monkeypatch.setattr(sim, "_native_runtime", lambda: runtime)
    monkeypatch.setattr(sim, "_PROGRAM_CACHE", OrderedDict())
    monkeypatch.setattr(native_simulation, "_PROGRAM_CACHE", OrderedDict())
    return runtime


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "cache.zhl"
    source.write_text(
        "module CacheCounter { clock clk reset rst out y:u8 "
        "reg q:u8=0 q <- truncate<8>(q+1) y=q }",
        encoding="utf-8",
    )
    return source


@pytest.mark.parametrize("typed_first", (False, True))
def test_source_and_typed_module_entry_points_share_exact_native_code(
    tmp_path, runtime, typed_first,
) -> None:
    source = _source(tmp_path)
    typed = native_simulation._program(compile_file(source).ir) if typed_first else None
    first = sim.compile(source, top="CacheCounter")
    typed = typed or native_simulation._program(first.module)
    logic = sim.compile(source, top="CacheCounter", logic_state=True)
    assert typed.plan.execution_identity == first.plan.execution_identity
    assert typed._native is first._native is logic._native
    assert runtime.calls == 1
    with first.create() as binary, logic.create() as four_state:
        assert binary.run_cycles("clk", 3) == {"y": 3}
        assert four_state.run_cycles("clk", 2)["y"].require_binary() == 2


def test_state_access_session_reuses_compiled_plan(tmp_path, runtime) -> None:
    source = _source(tmp_path)
    program = sim.compile(source, top="CacheCounter")
    result = compile_file(source, top="CacheCounter")
    state = SimulationStateSession(result)
    try:
        assert state._simulator.program._native is program._native
    finally:
        state._simulator.close()
    assert runtime.calls == 1


def test_same_execution_reuses_code_but_keeps_current_module_provenance(
    tmp_path, runtime,
) -> None:
    source = _source(tmp_path)
    first = sim.compile(source, top="CacheCounter")
    source.write_text("// shifted source origins\n" + source.read_text())
    second = sim.compile(source, top="CacheCounter")
    typed = native_simulation._program(second.module)
    assert first.plan.identity != second.plan.identity
    assert typed.module is second.module
    assert typed.plan.execution_identity == first.plan.execution_identity
    assert typed._native is first._native is second._native
    assert runtime.calls == 1


def test_native_cache_serializes_same_plan_compilation(tmp_path, runtime) -> None:
    plan = build_simulation_plan(compile_file(_source(tmp_path)).ir)
    with ThreadPoolExecutor(max_workers=8) as pool:
        compiled = list(pool.map(lambda _: sim._compiled_native_program(plan), range(16)))
    assert all(program is compiled[0] for program in compiled)
    assert runtime.calls == 1


def test_native_cache_is_lru_bounded(tmp_path, runtime, monkeypatch) -> None:
    monkeypatch.setattr(sim, "_PROGRAM_CACHE_ENTRIES", 2)
    source = _source(tmp_path)
    first = sim.compile(source)
    source.write_text(source.read_text().replace("q+1", "q+2"))
    second = sim.compile(source)
    assert sim._compiled_native_program(first.plan) is first._native
    source.write_text(source.read_text().replace("q+2", "q+3"))
    sim.compile(source)
    assert len(sim._PROGRAM_CACHE) == 2
    assert sim._compiled_native_program(first.plan) is first._native
    assert sim._compiled_native_program(second.plan) is not second._native
    assert runtime.calls == 4


def test_cached_program_still_requires_compatible_runtime(
    tmp_path, runtime, monkeypatch,
) -> None:
    program = sim.compile(_source(tmp_path))

    def incompatible():
        raise JitUnsupportedFeatureError("native runtime ABI is incompatible")

    monkeypatch.setattr(sim, "_native_runtime", incompatible)
    with pytest.raises(JitUnsupportedFeatureError, match="ABI is incompatible"):
        sim._compiled_native_program(program.plan)
    assert runtime.calls == 1


def test_typed_module_preserves_runtime_unavailable_error(tmp_path, monkeypatch) -> None:
    module = compile_file(_source(tmp_path)).ir

    def unavailable():
        raise JitUnsupportedFeatureError("native runtime is not installed")

    monkeypatch.setattr(sim, "_native_runtime", unavailable)
    with pytest.raises(JitUnsupportedFeatureError, match="runtime is not installed"):
        native_simulation._program(module)


def test_failed_native_compilation_is_not_cached(tmp_path, runtime, monkeypatch) -> None:
    plan = build_simulation_plan(compile_file(_source(tmp_path)).ir)
    compile_plan = runtime.compile_plan_bytes

    def fail(_payload):
        raise ValueError("invalid executable plan")

    monkeypatch.setattr(runtime, "compile_plan_bytes", fail)
    with pytest.raises(ValueError, match="invalid executable plan"):
        sim._compiled_native_program(plan)
    assert not sim._PROGRAM_CACHE
    monkeypatch.setattr(runtime, "compile_plan_bytes", compile_plan)
    sim._compiled_native_program(plan)
    assert runtime.calls == 1
