# ZLang HDL VS Code grammar

This repository-owned extension provides lexical highlighting for the supported
ZLang HDL surface. It is intentionally not an LSP, formatter, completion engine,
or semantic validator: a highlighted token is not proof that a program is
well-typed.

It registers canonical `.zhl` files with VS Code language id `zlang-hdl`.
The TextMate scope remains `source.zlang` for theme compatibility.

The grammar covers the current normalization-only concise forms as ordinary
lexical syntax: grouped ports (`in a, b : u8`), inline value outputs
(`out y : u9 = a + b`), and complete struct field puns (`Pair { left right }`).
Their semantic expansion and diagnostics are provided by the compiler, not by
this lexical extension.

Fixed hardware text and structural tuples are part of the same bounded surface.
`char`/`string<N>` and character/string literals have distinct type and literal
scopes, including exact byte escapes and invalid-escape highlighting. Tuple
types, values, literal projections, and flat destructuring use the ordinary
parenthesis, comma, index, type, and binding scopes; highlighting does not imply
runtime tuple selection or general pattern matching.

Typed bit and collection manipulation is also lexically distinguished:
`x[MSB:LSB]` uses dedicated slice punctuation, `bits_value[index]` is the
compile-time-selected LSB-zero packed-bit form, `concat(...)`/`parity(...)` and
the exact packed constants `zeros<N>`/`ones<N>` are hardware intrinsics, and
`reshape(...)`/`bitcast<T>(...)` are conversion intrinsics. Low-level
`pack(...)`/`unpack<T>(...)` compatibility forms remain highlighted.
Highlighting does not waive compile-time-shape, exact-width, or non-enum
representation requirements.

The same compiler-owned surface includes contextual `repeat(value)`, immutable
`value with { ... }` struct update, vector literals, structural aggregate
comparison, qualified-initial `fsm state = Enum.Initial`, and adjacent
`clock clk reset rst` declarations. Bounded option-free named-interface
connection chains use the ordinary `->` scopes; highlighting does not prove the
intermediate instances have one unambiguous protocol input/output. Exhaustive
immutable nominal-struct destructuring
(`Struct { field, ... } = value`) is current syntax; partial patterns and field
renaming are not.

Exact concise lowering also covers contextual `truncate(expr)` /
`extend(expr)`, static half-open vector ranges `values[first..past_last]`,
declaration-ordered parameter defaults such as `IW=index_width(N)`, and
`priority high > medium > low`. The editor highlights these as the existing
conversion, range, compile-time intrinsic, and scheduling categories; only the
compiler can validate exact target widths, static bounds, parameter order, and
the referenced rule names.

Physical single-domain declarations are highlighted as a distinct contract:
`clock clk { edge rising|falling }`, concise `async reset arst @clk`, and the
low-level compatibility form `reset rst @clk { mode ... polarity ... power_up
... }`. The directive names and the accepted synchronous, asynchronous,
active-high/low, and power-up values have separate TextMate scopes. Legacy
`clock clk` / `reset rst @clk` remains highlighted unchanged.

First-class verification declarations have declaration-sensitive scopes:
standalone `assert name [@ clk] { ... }` and `cover name [@ clk] { ... }`, plus
`contract name [@ clk] { require/assert/ensure/cover ... }`. Goal and scope
names retain property/scope name scopes, `@` remains an annotation operator,
and clock names remain signal references. These words are contextual: outside
the corresponding declaration header, same-spelled ports and immutable
bindings are ordinary identifiers. Highlighting never claims that a predicate
is bindable or proved; `zlang --check` and `zlang --verify` provide those
separate semantic and execution checks.

Nominal tagged unions use `union`, qualified variants and exhaustive pure
`match`. Union type names, variant declarations/references, payload field
declarations, and match binders are lexically distinct. Highlighting does not
imply wildcard, guarded, partial, nested, or runtime-procedural matching.

Initialized storage declarations are covered as a distinct public surface:
`rom table : rom<T,N> { read_latency 1 init expression }` highlights the ROM
resource name, storage type, and `read_latency`/`init` directives separately.
The grammar remains lexical; compile-time initializer legality and exact
one-cycle behavior are enforced by `zlang`.

Version 0.0.9 assigns separate TextMate scopes to declaration names, port and
state declarations, rule labels, nominal/builtin/generic types, generic type
arguments, intrinsic and user function calls, member access, configuration
modes, target metrics, operators, and numeric literals. Generic type delimiters
are punctuation rather than comparison operators, and a complete generic type
is no longer painted as one undifferentiated token. Actual colors remain under
the active VS Code theme, but neighboring semantic categories now receive
distinct standard scopes.

Typed compile-time parameters retain those distinctions too: `image : vec<N,T>`
uses the ordinary parameter/type scopes, `operation : fn(A) -> B` gives the
function-signature token its own scope, and `operation=fn widen` distinguishes
the static function reference from a runtime/user call. The protocol-aware
`transform` keyword uses the exploration/control scope; semantic eligibility is
still decided by `zlang`.

Function calls use three intentionally restrained styles:

- user `fn` calls use `entity.name.function.call.zlang` and retain the theme's
  normal function style;
- hardware/dataflow and conversion built-ins use
  `support.function.builtin.hardware.zlang` or
  `support.function.builtin.conversion.zlang` and are bold in the repository
  workspace;
- compile-time intrinsics and `equiv` guard predicates use
  `support.function.builtin.compile-time.zlang` or
  `support.function.guard.zlang` and are italic in the repository workspace.

The repository-owned recommended customization is tracked in
`recommended-settings.json`. The same scoped rules can be copied into user
settings when using ZLang outside this checkout; no workspace-local untracked
settings file is required.

Angle brackets are intentionally not registered as editor bracket pairs:
otherwise VS Code's bracket-pair colorizer splits `>=`, `<=`, `->`, and `=>`
despite each being one TextMate operator token. Generic/type angles remain
scoped by the grammar itself. Structural/relational operators and `@` are bold
and use one red foreground in the repository workspace. The same visual rule
also covers arithmetic, bitwise, logical, shift, assignment, ternary, range,
equivalence, and choice operators, while their distinct lexical scopes remain
available for inspection and tooling. This prevents themes from rendering a
multi-character operator as visually incomplete. Resource-only
directives such as `enable` are
scoped only inside a `resource` declaration, so a CSR field named `enable`
remains an ordinary field definition.

After all declaration, type, keyword, call, member, mode, and operator patterns,
remaining identifiers receive `variable.other.readwrite.zlang`. Consequently a
port has a definition scope in `in/out` declarations and a variable-reference
scope when it is used later in an expression; it no longer falls back to plain
source text. The repository workspace gives these signal/state references a
yellow foreground because several common themes otherwise render
`variable.other` exactly like unscoped source text. Port, local, and hardware
state definitions use that same foreground, so one symbol keeps one visual
identity between its declaration and every use even though definition/reference
scopes remain distinct for tooling.

Top-level named module-interface declarations receive a distinct
`entity.name.type.interface.zlang` scope. Module-body aggregate endpoint
declarations keep their hardware-port scope; the lexical distinction comes from
the `{` or generic-parameter list following a top-level interface name. As with
all extension highlighting, exact interface conformance is checked only by
`zlang --check`.

For local testing, install/update it with:

```sh
mkdir -p ~/.vscode/extensions/zlang-hdl
cp -r editors/vscode/zlang-hdl/. ~/.vscode/extensions/zlang-hdl/
```

Reload VS Code after updating the copy. The tracked files in this directory are
authoritative; the installed home-directory copy is only a deployment target.

If an older installed copy remains active, check that the extension version is
`0.0.9` and run **Developer: Reload Window** after copying it.
