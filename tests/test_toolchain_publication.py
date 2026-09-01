from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from zlang import compile_source
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.toolchain import ToolchainError, generate_verilog


def test_clash_generation_stages_outputs_and_does_not_claim_stale_verilog(
    tmp_path: Path, monkeypatch,
) -> None:
    output = tmp_path / "rtl"
    output.mkdir()
    stale = output / "stale.v"
    stale.write_text("module Stale; endmodule\n", encoding="utf-8")

    def fake_run(command, **_kwargs):
        staged = Path(command[command.index("-outputdir") + 1])
        assert staged.resolve() != output.resolve()
        generated = staged / "Top.topEntity" / "Top.v"
        generated.parent.mkdir(parents=True)
        generated.write_text("module Top; endmodule\n", encoding="utf-8")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("zlang.toolchain.subprocess.run", fake_run)
    generated = generate_verilog(
        "module Top where\n", "Top", output, "/tool/clash"
    )

    current = output / "Top.topEntity" / "Top.v"
    assert generated == (current,)
    assert current.read_text(encoding="utf-8") == "module Top; endmodule\n"
    assert stale.read_text(encoding="utf-8") == "module Stale; endmodule\n"
    assert stale not in generated


def test_clash_generation_uses_typed_prefix_and_publishes_public_wrapper(
    tmp_path: Path, monkeypatch,
) -> None:
    compilation = compile_source(
        "module VecTop { in a:vec<2,u8> out y:vec<2,u8> y=a }"
    )
    wrapper = ClashPublicTopWrapper.build(compilation.ir)
    output = tmp_path / "rtl"

    def fake_run(command, **_kwargs):
        assert command[-2:] == ["-fclash-component-prefix", "zlang_core"]
        staged = Path(command[command.index("-outputdir") + 1])
        generated = staged / "VecTop.topEntity" / "zlang_core_VecTop.v"
        generated.parent.mkdir(parents=True)
        generated.write_text(
            "module zlang_core_VecTop(input [15:0] a, output [15:0] y); "
            "assign y=a; endmodule\n",
            encoding="utf-8",
        )
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("zlang.toolchain.subprocess.run", fake_run)
    generated = generate_verilog(
        compilation.clash,
        "VecTop",
        output,
        "/tool/clash",
        public_wrapper=wrapper,
    )

    assert tuple(path.relative_to(output).as_posix() for path in generated) == (
        "VecTop.sv",
        "VecTop.topEntity/zlang_core_VecTop.v",
    )
    assert (output / "VecTop.sv").read_text() == wrapper.text


def test_clash_generation_rejects_destination_symlink_without_touching_target(
    tmp_path: Path, monkeypatch,
) -> None:
    output = tmp_path / "rtl"
    destination = output / "Top.topEntity" / "Top.v"
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside.v"
    outside.write_text("module Untouched; endmodule\n", encoding="utf-8")
    destination.symlink_to(outside)

    def fake_run(command, **_kwargs):
        staged = Path(command[command.index("-outputdir") + 1])
        generated = staged / "Top.topEntity" / "Top.v"
        generated.parent.mkdir(parents=True)
        generated.write_text("module Top; endmodule\n", encoding="utf-8")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("zlang.toolchain.subprocess.run", fake_run)
    with pytest.raises(ToolchainError, match="destination is a symbolic link"):
        generate_verilog("module Top where\n", "Top", output, "/tool/clash")

    assert outside.read_text(encoding="utf-8") == "module Untouched; endmodule\n"
    assert destination.is_symlink()


def test_clash_generation_rejects_symlinked_destination_directory_escape(
    tmp_path: Path, monkeypatch,
) -> None:
    output = tmp_path / "rtl"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / "Top.topEntity").symlink_to(outside, target_is_directory=True)

    def fake_run(command, **_kwargs):
        staged = Path(command[command.index("-outputdir") + 1])
        generated = staged / "Top.topEntity" / "Top.v"
        generated.parent.mkdir(parents=True)
        generated.write_text("module Top; endmodule\n", encoding="utf-8")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("zlang.toolchain.subprocess.run", fake_run)
    with pytest.raises(ToolchainError, match="symbolic link or non-directory"):
        generate_verilog("module Top where\n", "Top", output, "/tool/clash")

    assert not (outside / "Top.v").exists()


def test_clash_generation_rejects_symlinked_publication_root(
    tmp_path: Path, monkeypatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "rtl"
    output.symlink_to(outside, target_is_directory=True)

    def fake_run(command, **_kwargs):
        staged = Path(command[command.index("-outputdir") + 1])
        generated = staged / "Top.topEntity" / "Top.v"
        generated.parent.mkdir(parents=True)
        generated.write_text("module Top; endmodule\n", encoding="utf-8")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("zlang.toolchain.subprocess.run", fake_run)
    with pytest.raises(ToolchainError, match="symbolic link or non-directory"):
        generate_verilog("module Top where\n", "Top", output, "/tool/clash")

    assert not (outside / "Top.topEntity" / "Top.v").exists()
