"""Compiler-owned call/signature projections for editor tooling."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.source import SourceOrigin


@dataclass(frozen=True)
class SignatureParameter:
    """One resolved callable parameter rendered for signature help."""

    name: str
    type_text: str

    def __post_init__(self) -> None:
        if not self.name or not self.type_text:
            raise ValueError("signature parameter name and type are required")

    @property
    def label(self) -> str:
        return f"{self.name} : {self.type_text}"


@dataclass(frozen=True)
class SignatureHelpCall:
    """One successfully resolved call and its authoritative argument spans."""

    call_origin: SourceOrigin
    callee_origin: SourceOrigin | None
    argument_origins: tuple[SourceOrigin | None, ...]
    name: str
    parameters: tuple[SignatureParameter, ...]
    return_type: str

    def __post_init__(self) -> None:
        if not self.name or not self.return_type:
            raise ValueError("signature call name and return type are required")
        if len(self.argument_origins) != len(self.parameters):
            raise ValueError(
                "signature call arguments and parameters must have equal arity"
            )

    @property
    def label(self) -> str:
        parameters = ", ".join(parameter.label for parameter in self.parameters)
        return f"fn {self.name}({parameters}) -> {self.return_type}"


__all__ = ["SignatureHelpCall", "SignatureParameter"]
