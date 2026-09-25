from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from tools.materialize_native_test_extension import materialize


def _wheel(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return path


def test_materialize_extracts_only_the_native_extension(tmp_path: Path) -> None:
    wheel = _wheel(
        tmp_path / "zlang_hdl-0.1.0a16-cp312-abi3-linux_x86_64.whl",
        {
            "_zlang_native_sim/__init__.py": b"from ._zlang_native_sim import *\n",
            "_zlang_native_sim/_zlang_native_sim.abi3.so": b"native",
            "zlang/__init__.py": b"must not be extracted",
        },
    )
    destination = tmp_path / "source"

    target = materialize(wheel, destination)

    assert target == (
        destination.resolve()
        / "_zlang_native_sim"
        / "_zlang_native_sim.abi3.so"
    )
    assert target.read_bytes() == b"native"
    assert (destination / "_zlang_native_sim/__init__.py").is_file()
    assert not (destination / "zlang/__init__.py").exists()


@pytest.mark.parametrize(
    "members",
    [
        {},
        {
            "_zlang_native_sim/__init__.py": b"package",
            "_zlang_native_sim/_zlang_native_sim.abi3.so": b"one",
            "_zlang_native_sim/_zlang_native_sim.other.abi3.so": b"two",
        },
    ],
)
def test_materialize_requires_exactly_one_extension(
    tmp_path: Path, members: dict[str, bytes]
) -> None:
    wheel = _wheel(tmp_path / "bad.whl", members)

    with pytest.raises(SystemExit, match="exactly one importable"):
        materialize(wheel, tmp_path / "source")
