from __future__ import annotations

from functools import cache
from importlib.resources import files
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

import zlang.parser.parser as parser_module
from zlang.ast.nodes import AddExpr, NameExpr, NumberExpr
from zlang.source import SourceSpan


ROOT = Path(__file__).resolve().parents[2]
SOURCE = "module Simple { in a:u8 out y:u8 y=a }"


def _run_python(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *arguments],
        cwd=ROOT,
        env=dict(os.environ, COLUMNS="80", LC_ALL="C"),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    return result


@pytest.fixture
def constructions(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    constructed = []
    real_lark = parser_module.Lark
    monkeypatch.setattr(parser_module, "_PARSER", None)
    # Isolate probe misses without clearing a cache belonging to other tests.
    monkeypatch.setattr(
        parser_module,
        "_ordinary_binding_name_is_valid",
        cache(parser_module._ordinary_binding_name_is_valid.__wrapped__),
    )

    def construct(*args, **kwargs):
        assert args == (parser_module._GRAMMAR,)
        assert kwargs == {"parser": "lalr", "propagate_positions": True}
        assert parser_module._GRAMMAR == (
            files("zlang.parser").joinpath("grammar.lark").read_text()
        )
        parser = real_lark(*args, **kwargs)
        constructed.append(parser)
        return parser

    monkeypatch.setattr(parser_module, "Lark", construct)
    return constructed


def test_concurrent_first_parse_and_tuple_probes_share_one_parser() -> None:
    # A child bounds deadlocks, including executor shutdown after a failed test.
    result = _run_python(
        r"""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, current_thread, local, main_thread

        import zlang.parser.parser as parser_module
        from zlang.source import SourceSpan

        assert parser_module._PARSER is None
        parser_module._ordinary_binding_name_is_valid.cache_clear()
        real_lark = parser_module.Lark
        calls = []
        start = Barrier(8, timeout=15)
        entering_parse = Barrier(8, timeout=15)
        per_thread = local()

        def construct(*args, **kwargs):
            calls.append((args, kwargs))
            parser = real_lark(*args, **kwargs)
            real_parse = parser.parse

            def checked_parse(*args, **kwargs):
                # Parsing must not hold the constructor lock, including probes
                # called recursively during tuple AST transformation.
                assert parser_module._PARSER_LOCK.acquire(timeout=15)
                parser_module._PARSER_LOCK.release()
                if current_thread() is not main_thread() and not getattr(
                    per_thread, "entered_parse", False
                ):
                    per_thread.entered_parse = True
                    entering_parse.wait()
                return real_parse(*args, **kwargs)

            parser.parse = checked_parse
            return parser

        parser_module.Lark = construct

        def work(index):
            start.wait()
            if index % 2:
                name = "when" if index == 1 else f"probe_{index}"
                return parser_module._ordinary_binding_name_is_valid(name)
            return parser_module.parse(
                f"module Concurrent{index} {{\n"
                " in p:(u8,bit)\n out y:u8\n"
                f" (data_{index},last_{index})=p\n y=data_{index}\n}}"
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(work, range(8)))

        assert calls == [
            ((parser_module._GRAMMAR,),
             {"parser": "lalr", "propagate_positions": True})
        ]
        modules = results[::2]
        assert len({id(module) for module in modules}) == 4
        for index, module in zip(range(0, 8, 2), modules):
            assert module.name == f"Concurrent{index}"
            expression = module.assignments[0].expression
            assert expression.name == f"data_{index}"
            assert expression.origin == SourceSpan(5, 4, 5, 10)
        assert results[1::2] == [False, True, True, True]
        assert parser_module.parse("module Warm { out y:u1 y=0 }").name == "Warm"
        assert len(calls) == 1
        """
    )
    assert result.stdout == ""


@pytest.mark.parametrize("first_consumer", ("parse", "probe"))
def test_failed_construction_can_retry_without_publishing_partial_parser(
    first_consumer: str,
    constructions: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    construct = parser_module.Lark
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected parser construction failure")
        return construct(*args, **kwargs)

    monkeypatch.setattr(parser_module, "Lark", fail_once)
    consume = (
        (lambda: parser_module.parse(SOURCE))
        if first_consumer == "parse"
        else (lambda: parser_module._ordinary_binding_name_is_valid("retry_name"))
    )
    with pytest.raises(RuntimeError, match="injected parser construction failure"):
        consume()
    assert parser_module._PARSER is None
    assert not parser_module._PARSER_LOCK.locked()
    assert constructions == []

    consume()
    assert parser_module.parse(SOURCE).name == "Simple"
    assert attempts == 2
    assert len(constructions) == 1
    assert parser_module._PARSER is constructions[0]


def test_tuple_probe_can_initialize_parser_and_preserves_its_cache(
    constructions: list[object],
) -> None:
    probe = parser_module._ordinary_binding_name_is_valid
    assert probe("initial_probe")
    assert probe("initial_probe")
    assert not probe("when")
    assert not probe("when")
    assert (probe.cache_info().hits, probe.cache_info().misses) == (2, 2)

    module = parser_module.parse(
        "module TupleProbe { in p:(u8,bit) out y:u8 "
        "(initial_probe,last)=p y=initial_probe }"
    )
    assert module.assignments[0].expression.name == "initial_probe"
    assert len(constructions) == 1
    assert parser_module._PARSER is constructions[0]


def test_cold_and_warm_parses_keep_distinct_asts_and_exact_nested_positions(
    constructions: list[object],
) -> None:
    source = "module Positions {\n in a:u8\n out y:u9\n y=a+1 // expression\n}"
    cold = parser_module.parse(source)
    warm = parser_module.parse(source)

    assert cold == warm
    assert cold is not warm
    for module in (cold, warm):
        expression = module.assignments[0].expression
        assert isinstance(expression, AddExpr)
        assert isinstance(expression.left, NameExpr)
        assert isinstance(expression.right, NumberExpr)
        assert expression.origin == SourceSpan(4, 4, 4, 7)
        assert expression.left.origin == SourceSpan(4, 4, 4, 5)
        assert expression.right.origin == SourceSpan(4, 6, 4, 7)
    assert cold.assignments[0].expression is not warm.assignments[0].expression
    assert len(constructions) == 1


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module Bad {\n out y:u8\n y=\n}",
            "syntax error at line 4, column 1: }\n^",
        ),
        (
            "module Bad { in p:(u8,bit) out y:bit (when,last)=p y=last }",
            "tuple binding 'when' is not a legal immutable binding name",
        ),
    ),
)
def test_cold_and_warm_diagnostics_do_not_discard_initialized_parser(
    source: str, message: str, constructions: list[object],
) -> None:
    for _ in range(2):
        with pytest.raises(parser_module.ParseError) as raised:
            parser_module.parse(source)
        assert str(raised.value) == message
        assert len(constructions) == 1
        assert parser_module._PARSER is constructions[0]
    assert parser_module.parse(SOURCE).name == "Simple"
    assert len(constructions) == 1


@pytest.mark.parametrize("flag", ("--help", "--version"))
def test_cold_cli_help_and_version_do_not_construct_parser_or_probe_tools(
    flag: str,
) -> None:
    guarded = _run_python(
        """
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        import shutil
        import sys

        def forbidden(*args, **kwargs):
            raise AssertionError("help/version triggered construction or discovery")

        shutil.which = forbidden

        def no_processes(event, args):
            if event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
                forbidden()

        sys.addaudithook(no_processes)
        import lark
        real_init = lark.Lark.__init__
        lark.Lark.__init__ = forbidden
        # Install guards before importing any zlang module: its package import
        # reaches the parser even when entering through the public CLI.
        from zlang.cli import main
        import zlang.parser.parser as parser_module

        def output():
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    main([sys.argv[1]])
                except SystemExit as error:
                    assert error.code == 0
                else:
                    raise AssertionError("help/version did not exit")
            assert stderr.getvalue() == ""
            return stdout.getvalue()

        assert parser_module._PARSER is None
        cold = output()
        assert parser_module._PARSER is None
        lark.Lark.__init__ = real_init
        parser_module.parse("module Warm { out y:u1 y=0 }")
        lark.Lark.__init__ = forbidden
        assert output() == cold
        sys.stdout.write(cold)
        """,
        flag,
    )
    ordinary = _run_python(
        """
        import runpy
        import sys
        sys.argv = ["zlang", sys.argv[1]]
        runpy.run_module("zlang.cli", run_name="__main__")
        """,
        flag,
    )
    assert guarded.stdout == ordinary.stdout
    if flag == "--help":
        assert guarded.stdout.startswith("usage: zlang ")
    else:
        from zlang._version import __version__

        assert guarded.stdout == f"zlang {__version__}\n"
