"""Physical, nonsemantic inputs consulted by one compilation session."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable


def _physical_paths(paths: Iterable[Path | str]) -> tuple[Path, ...]:
    return tuple(
        sorted(
            {Path(path).expanduser().resolve(strict=True) for path in paths},
            key=lambda path: path.as_posix(),
        )
    )


@dataclass(frozen=True)
class PhysicalCompilationInputs:
    """Host paths used for overwrite prevention and diagnostics only."""

    root_source: Path | None = None
    project_manifest: Path | None = None
    project_lock: Path | None = None
    project_module_sources: tuple[Path, ...] = ()
    dependency_manifests: tuple[Path, ...] = ()
    dependency_module_sources: tuple[Path, ...] = ()
    stdlib_sources: tuple[Path, ...] = ()
    external_sources: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        for attribute in ("root_source", "project_manifest", "project_lock"):
            value = getattr(self, attribute)
            if value is not None:
                object.__setattr__(
                    self, attribute, Path(value).expanduser().resolve(strict=True)
                )
        for attribute in (
            "project_module_sources",
            "dependency_manifests",
            "dependency_module_sources",
            "stdlib_sources",
            "external_sources",
        ):
            object.__setattr__(
                self, attribute, _physical_paths(getattr(self, attribute))
            )

    @property
    def all_paths(self) -> tuple[Path, ...]:
        optional = (self.root_source, self.project_manifest, self.project_lock)
        return tuple(
            sorted(
                {
                    *(path for path in optional if path is not None),
                    *self.project_module_sources,
                    *self.dependency_manifests,
                    *self.dependency_module_sources,
                    *self.stdlib_sources,
                    *self.external_sources,
                },
                key=lambda path: path.as_posix(),
            )
        )

    def with_stdlib_sources(
        self,
        paths: Iterable[Path | str],
    ) -> "PhysicalCompilationInputs":
        return replace(
            self,
            stdlib_sources=_physical_paths((*self.stdlib_sources, *tuple(paths))),
        )


__all__ = ["PhysicalCompilationInputs"]
