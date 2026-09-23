from __future__ import annotations

import pytest

from zlang.common.graph import (
    DependencyCycle,
    ReachabilityIndex,
    dependency_postorder,
    reachable,
)


def test_dependency_postorder_is_stable_dependency_first_and_unique() -> None:
    graph = {
        "root-a": ("shared", "leaf-a"),
        "root-b": ("shared", "leaf-b"),
        "shared": ("base",),
        "leaf-a": (),
        "leaf-b": (),
        "base": (),
    }
    assert dependency_postorder(
        ("root-a", "root-b"), graph.__getitem__
    ) == ("base", "shared", "leaf-a", "root-a", "leaf-b", "root-b")


def test_dependency_cycle_retains_one_closed_stable_path() -> None:
    graph = {"a": ("b",), "b": ("c",), "c": ("b",)}
    with pytest.raises(DependencyCycle) as caught:
        dependency_postorder(("a",), graph.__getitem__)
    assert caught.value.nodes == ("b", "c", "b")
    assert str(caught.value) == "b -> c -> b"


def test_reachable_terminates_on_cycles() -> None:
    graph = {"a": ("b",), "b": ("a", "c"), "c": ()}
    assert reachable("a", "c", graph.__getitem__)
    assert not reachable("c", "a", graph.__getitem__)


def test_reachability_index_answers_repeated_queries_and_detects_cycles() -> None:
    acyclic = ReachabilityIndex({("a", "b"), ("b", "c"), ("x", "y")})
    assert acyclic.reaches("a", "c")
    assert not acyclic.reaches("c", "a")
    assert acyclic.reaches("missing", "missing")
    assert not acyclic.has_cycle

    cyclic = ReachabilityIndex({("a", "b"), ("b", "c"), ("c", "a")})
    assert cyclic.has_cycle
