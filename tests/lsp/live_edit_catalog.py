"""Single test-owned catalog for equivalent live-edit source spellings."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageSurfaceCase:
    surface_id: str
    explicit: str
    concise: str


LIVE_EDIT_CASES = (
    LanguageSurfaceCase(
        "clock-reset-adjacent",
        "module Top { clock clk reset rst reg x:u8=0 rule tick when 1 { x <- x } out y:u8 y=x }",
        "module Top { clock clk\nreset rst\nreg x:u8=0 rule tick when 1 { x <- x } out y:u8 y=x }",
    ),
    LanguageSurfaceCase(
        "async-reset-defaults",
        "module Top { clock clk async reset arst @clk { polarity active_high } }",
        "module Top { clock clk async reset arst @clk }",
    ),
    LanguageSurfaceCase(
        "instance-keyword",
        "module Child { in x:u8 out y:u8 y=x } module Top { in x:u8 out y:u8 inst child:Child { x } y=child.y }",
        "module Child { in x:u8 out y:u8 y=x } module Top { in x:u8 out y:u8 child:Child { x } y=child.y }",
    ),
    LanguageSurfaceCase(
        "interface-keyword",
        "protocol P { role source role sink channel data:rv<u8> source -> sink } module Top { clock clk reset rst interface p:P.source @clk }",
        "protocol P { role source role sink channel data:rv<u8> source -> sink } module Top { clock clk reset rst p:P.source @clk }",
    ),
    LanguageSurfaceCase(
        "connect-keyword",
        "module Top { clock clk reset rst in source:rv<u8> out sink:rv<u8> connect source -> sink }",
        "module Top { clock clk reset rst in source:rv<u8> out sink:rv<u8> source -> sink }",
    ),
    LanguageSurfaceCase(
        "labeled-rule",
        "module Top { clock clk reset rst in go:bit reg x:u8=0 rule tick when go { x <- 1 } out y:u8 y=x }",
        "module Top { clock clk reset rst in go:bit reg x:u8=0 tick: when go { x <- 1 } out y:u8 y=x }",
    ),
    LanguageSurfaceCase(
        "priority-block",
        "module Top { clock clk reset rst in a,b:bit out y:u8 reg x:u8=0 rule hi when a { x <- 1 } rule lo when b { x <- 2 } priority hi > lo y=x }",
        "module Top { clock clk reset rst in a,b:bit out y:u8 reg x:u8=0 priority { hi: when a { x <- 1 } lo: when b { x <- 2 } } y=x }",
    ),
    LanguageSurfaceCase(
        "priority-chain",
        "module Top { clock clk reset rst in a,b,c:bit out y:u8 reg x:u8=0 a0: when a { x <- 1 } a1: when b { x <- 2 } a2: when c { x <- 3 } priority a0 > a1 priority a1 > a2 y=x }",
        "module Top { clock clk reset rst in a,b,c:bit out y:u8 reg x:u8=0 a0: when a { x <- 1 } a1: when b { x <- 2 } a2: when c { x <- 3 } priority a0 > a1 > a2 y=x }",
    ),
    LanguageSurfaceCase(
        "fsm-type-inference",
        "enum Phase { Idle Run } module Top { clock clk reset rst fsm state:Phase=Idle { Idle { -> Run {} } Run { hold } } }",
        "enum Phase { Idle Run } module Top { clock clk reset rst fsm state=Phase.Idle { Idle { -> Run {} } Run { hold } } }",
    ),
    LanguageSurfaceCase(
        "contextual-resize",
        "module Top { in x:u16 in s:s8 out y:u8 out wide:s16 y=truncate<8>(x) wide=extend<16>(s) }",
        "module Top { in x:u16 in s:s8 out y:u8 out wide:s16 y=truncate(x) wide=extend(s) }",
    ),
    LanguageSurfaceCase(
        "inferred-binding",
        "module Top { in x:u8 out y:u9 tmp:u9=x+1 y=tmp }",
        "module Top { in x:u8 out y:u9 tmp=x+1 y=tmp }",
    ),
    LanguageSurfaceCase(
        "inferred-function-return",
        "fn widen(x:u8)->u9 { x+1 } module Top { in x:u8 out y:u9 y=widen(x) }",
        "fn widen(x:u8) { x+1 } module Top { in x:u8 out y:u9 y=widen(x) }",
    ),
    LanguageSurfaceCase(
        "inline-output",
        "module Top { in a:u8 in b:u8 out y:u9 y=a+b }",
        "module Top { in a,b:u8 out y:u9=a+b }",
    ),
    LanguageSurfaceCase(
        "struct-field-punning",
        "struct Pair { left:u8 right:u8 } module Top { in left,right:u8 out y:Pair y=Pair { left=left right=right } }",
        "struct Pair { left:u8 right:u8 } module Top { in left,right:u8 out y:Pair y=Pair { left right } }",
    ),
    LanguageSurfaceCase(
        "struct-update",
        "struct Beat { data:u8 last:bit } module Top { in beat:Beat out y:Beat y=Beat { data=beat.data last=1 } }",
        "struct Beat { data:u8 last:bit } module Top { in beat:Beat out y:Beat y=beat with { last=1 } }",
    ),
    LanguageSurfaceCase(
        "struct-destructure",
        "struct Beat { data:u8 last:bit } module Top { in beat:Beat out y:Beat whole:Beat=beat data:u8=whole.data last:bit=whole.last y=Beat { data last } }",
        "struct Beat { data:u8 last:bit } module Top { in beat:Beat out y:Beat Beat { data, last } = beat y=Beat { data last } }",
    ),
    LanguageSurfaceCase(
        "contextual-vector-repeat",
        "module Top { out y:vec<3,u8> y=repeat<3>(7) }",
        "module Top { out y:vec<3,u8> y=repeat(7) }",
    ),
    LanguageSurfaceCase(
        "ternary-mux",
        "module Top { in select:bit in a,b:u8 out y:u8 y=mux(select,a,b) }",
        "module Top { in select:bit in a,b:u8 out y:u8 y=select ? a : b }",
    ),
    LanguageSurfaceCase(
        "single-clock-domain-inference",
        "module Top { clock clk reset rst @clk reg x:u8 @clk=0 rule tick @clk when 1 { x <- x } out y:u8 @clk y=x }",
        "module Top { clock clk reset rst @clk reg x:u8=0 rule tick when 1 { x <- x } out y:u8 @clk y=x }",
    ),
)


__all__ = ["LIVE_EDIT_CASES", "LanguageSurfaceCase"]
