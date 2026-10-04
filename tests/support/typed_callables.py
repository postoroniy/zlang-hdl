"""Current typed-callable constructors for hand-built IR fixtures."""

from zlang.ir.callables import CallableKind, CallableMetadata, stable_callee_identity
from zlang.ir.expressions import Expression
from zlang.ir.module import Function, FunctionParameter
from zlang.ir.types import HardwareType


def typed_function(
    name: str,
    parameters: tuple[FunctionParameter, ...],
    return_type: HardwareType,
    body: Expression,
    callee_identity: str | None = None,
    metadata: CallableMetadata | None = None,
) -> Function:
    """Build one fully identified callable without exercising removed defaults."""

    if metadata is None:
        identity = callee_identity or (
            "test:function:"
            + name
            + ":"
            + ",".join(str(parameter.type) for parameter in parameters)
            + "->"
            + str(return_type)
        )
        metadata = CallableMetadata(
            CallableKind.FUNCTION,
            name,
            f"tests:{name}",
            identity,
        )
    identity = callee_identity or stable_callee_identity(
        parameters, return_type, metadata
    )
    return Function(
        name=name,
        parameters=parameters,
        return_type=return_type,
        body=body,
        callee_identity=identity,
        metadata=metadata,
    )


__all__ = ["typed_function"]
