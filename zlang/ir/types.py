"""Canonical hardware types used by the typed IR."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FixedOverflowPolicy(str, Enum):
    """Overflow behavior when a fixed-point value is stored at narrower width."""

    WRAP = "wrap"
    SATURATE = "saturate"


@dataclass(frozen=True)
class BitType:
    width: int = field(default=1, init=False)

    def __str__(self) -> str:
        return "bit"


@dataclass(frozen=True)
class UIntType:
    width: int

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("an unsigned hardware type must have positive width")

    def __str__(self) -> str:
        return f"u{self.width}"


@dataclass(frozen=True)
class SIntType:
    width: int

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("a signed hardware type must have positive width")

    def __str__(self) -> str:
        return f"s{self.width}"


@dataclass(frozen=True)
class BitsType:
    width: int

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("a bit-vector hardware type must have positive width")

    def __str__(self) -> str:
        return f"bits<{self.width}>"


@dataclass(frozen=True)
class FixedType:
    """Signed two's-complement fixed point with ``fraction`` fractional bits."""

    width: int
    fraction: int
    overflow: FixedOverflowPolicy = FixedOverflowPolicy.WRAP

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("a fixed-point hardware type must have positive width")
        if self.fraction < 0 or self.fraction >= self.width:
            raise ValueError("fixed-point fractional width must satisfy 0 <= F < W")

    def __str__(self) -> str:
        family = "fixed_sat" if self.overflow is FixedOverflowPolicy.SATURATE else "fixed"
        return f"{family}<{self.width},{self.fraction}>"


@dataclass(frozen=True)
class UFixedType:
    """Unsigned fixed point with ``fraction`` fractional bits."""

    width: int
    fraction: int
    overflow: FixedOverflowPolicy = FixedOverflowPolicy.WRAP

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("an unsigned fixed-point hardware type must have positive width")
        if self.fraction < 0 or self.fraction >= self.width:
            raise ValueError("fixed-point fractional width must satisfy 0 <= F < W")

    def __str__(self) -> str:
        family = "ufixed_sat" if self.overflow is FixedOverflowPolicy.SATURATE else "ufixed"
        return f"{family}<{self.width},{self.fraction}>"


@dataclass(frozen=True)
class EnumType:
    """Nominal enumeration with an ordinal or explicit raw-bit ABI."""

    name: str
    members: tuple[str, ...]
    declaration_identity: str
    explicit_width: int | None = None
    explicit_codes: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("an enum type must have a name")
        if not self.members:
            raise ValueError(f"enum '{self.name}' must contain at least one member")
        if any(not member for member in self.members):
            raise ValueError(f"enum '{self.name}' members must have names")
        if len(self.members) != len(set(self.members)):
            raise ValueError(f"enum '{self.name}' members must be unique")
        if not self.declaration_identity:
            raise ValueError(f"enum '{self.name}' requires a declaration identity")
        if (self.explicit_width is None) != (self.explicit_codes is None):
            raise ValueError(
                f"enum '{self.name}' explicit width and codes must be provided together"
            )
        if self.explicit_width is not None:
            if self.explicit_width < 1:
                raise ValueError(f"enum '{self.name}' explicit width must be positive")
            assert self.explicit_codes is not None
            if len(self.explicit_codes) != len(self.members):
                raise ValueError(
                    f"enum '{self.name}' requires one explicit code per member"
                )
            if len(self.explicit_codes) != len(set(self.explicit_codes)):
                raise ValueError(f"enum '{self.name}' explicit codes must be unique")
            limit = 1 << self.explicit_width
            if any(code < 0 or code >= limit for code in self.explicit_codes):
                raise ValueError(
                    f"enum '{self.name}' explicit codes must fit bits<{self.explicit_width}>"
                )

    @property
    def width(self) -> int:
        return (
            self.explicit_width
            if self.explicit_width is not None
            else max(1, (len(self.members) - 1).bit_length())
        )

    @property
    def codes(self) -> tuple[int, ...]:
        return (
            self.explicit_codes
            if self.explicit_codes is not None
            else tuple(range(len(self.members)))
        )

    def member_code(self, member: str) -> int:
        try:
            return self.codes[self.members.index(member)]
        except ValueError as error:
            raise ValueError(
                f"enum '{self.name}' has no member '{member}'"
            ) from error

    def is_valid_code(self, value: int) -> bool:
        return value in self.codes

    def __str__(self) -> str:
        members = ",".join(
            self.members
            if self.explicit_codes is None
            else tuple(
                f"{member}={code}"
                for member, code in zip(
                    self.members, self.explicit_codes, strict=True
                )
            )
        )
        if self.explicit_width is not None:
            members += f":bits<{self.explicit_width}>"
        return f"enum<{self.name}:{members}@{self.declaration_identity}>"


@dataclass(frozen=True)
class StructField:
    name: str
    type: HardwareType


@dataclass(frozen=True)
class StructType:
    name: str
    fields: tuple[StructField, ...]

    @property
    def width(self) -> int:
        return sum(field.type.width for field in self.fields)

    def field(self, name: str) -> StructField | None:
        return next((field for field in self.fields if field.name == name), None)

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class TupleType:
    """Structural ordered hardware tuple with bounded source-level arity."""

    elements: tuple[HardwareType, ...]

    def __post_init__(self) -> None:
        if not 2 <= len(self.elements) <= 8:
            raise ValueError("a hardware tuple must contain between 2 and 8 elements")

    @property
    def width(self) -> int:
        return sum(element.width for element in self.elements)

    def __str__(self) -> str:
        return f"({','.join(str(element) for element in self.elements)})"


@dataclass(frozen=True)
class TaggedUnionField:
    """One source-ordered payload field of a nominal union variant."""

    name: str
    type: HardwareType

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a tagged-union field must have a name")


@dataclass(frozen=True)
class TaggedUnionVariant:
    """One source-ordered variant in a nominal tagged union."""

    name: str
    fields: tuple[TaggedUnionField, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a tagged-union variant must have a name")
        names = tuple(field.name for field in self.fields)
        if len(names) != len(set(names)):
            raise ValueError(
                f"tagged-union variant '{self.name}' fields must be unique"
            )

    @property
    def payload_width(self) -> int:
        return sum(field.type.width for field in self.fields)

    def field(self, name: str) -> TaggedUnionField | None:
        return next((field for field in self.fields if field.name == name), None)


@dataclass(frozen=True)
class TaggedUnionType:
    """Nominal source-ordered tagged union with one frozen packed layout."""

    name: str
    variants: tuple[TaggedUnionVariant, ...]
    declaration_identity: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a tagged-union type must have a name")
        if not self.variants:
            raise ValueError(f"tagged union '{self.name}' must have variants")
        names = tuple(variant.name for variant in self.variants)
        if len(names) != len(set(names)):
            raise ValueError(f"tagged union '{self.name}' variants must be unique")
        unsupported = next(
            (
                field
                for variant in self.variants
                for field in variant.fields
                if not isinstance(
                    field.type,
                    (
                        BitType,
                        BitsType,
                        UIntType,
                        SIntType,
                        FixedType,
                        UFixedType,
                    ),
                )
            ),
            None,
        )
        if unsupported is not None:
            raise ValueError(
                f"tagged union '{self.name}' field '{unsupported.name}' must "
                "be a flat scalar type"
            )
        if not self.declaration_identity:
            raise ValueError(
                f"tagged union '{self.name}' requires a declaration identity"
            )

    @property
    def tag_width(self) -> int:
        return max(1, (len(self.variants) - 1).bit_length())

    @property
    def payload_width(self) -> int:
        return max(variant.payload_width for variant in self.variants)

    @property
    def width(self) -> int:
        return self.tag_width + self.payload_width

    def variant(self, name: str) -> TaggedUnionVariant | None:
        return next((variant for variant in self.variants if variant.name == name), None)

    def tag(self, name: str) -> int:
        for index, variant in enumerate(self.variants):
            if variant.name == name:
                return index
        raise ValueError(f"tagged union '{self.name}' has no variant '{name}'")

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class VecType:
    length: int
    element_type: HardwareType

    def __post_init__(self) -> None:
        if self.length < 1:
            raise ValueError("a vector must have positive length")

    @property
    def width(self) -> int:
        return self.length * self.element_type.width

    def __str__(self) -> str:
        return f"vec<{self.length},{self.element_type}>"


HardwareType = (
    BitType | UIntType | SIntType | BitsType | FixedType | UFixedType |
    EnumType | StructType | TupleType | TaggedUnionType | VecType
)
