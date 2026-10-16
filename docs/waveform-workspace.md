# Waveform workspace

The Linux-x64 extension displays saved VCD/FST files and native debugger traces
with the same waveform controls. Install matching compiler, native runtime,
and extension binaries. The universal VSIX provides editor/LSP support only.

A saved `.fst` or `.vcd` tab displays a file snapshot. It does not attach to the
debugger or follow simulation steps. Use **ZLang HDL: Open Live Waveform** to
observe the active ZLang debug session; use **Reload** to reread a changed file.

## Refresh a simulation result

When a simulator writes or replaces an open FST/VCD file, **File changed** appears
beside the highlighted **Reload** button. Wait for simulation to finish, then
click **Reload**. Signals, duplicate rows, colors, groups, and viewports are kept;
cursors and ranges are clamped if the new trace is shorter. Reload updates all
editors of the same trace. It does not reread a partially written file
automatically; native queries pause until Reload so old and newly written data
are not mixed. If reading fails, the previous view remains and Reload can retry.

Signals absent from the new catalog remain as removable **holder** rows. Their
previously loaded diagrams are faded and explicitly marked `holder`; they are
not live/current samples and cannot drive navigation or measurements. **×**
deletes a holder. Exact name-and-width reappearance rebinds the original row on
the next reload.

Only the last loaded visible tiles are retained, with a 16 MiB accounting cap
per editor. Outside those ranges, or after eviction/reopening a session, the
holder reports that its snapshot is unavailable. Sessions still store no
waveform samples. In a live debugger trace the button is **Refresh**: it rereads
recorded history without executing an event, resetting state, or replaying inputs.

Live debugger traces include the design's clocks and resets automatically.
Their waveforms use the declared clock edge and reset polarity. They can also
be added from **Variables → Clocks and resets → Add to Waveform** or the live
signal picker. Native trace ticks describe atomic events, not hardware time:
reset changes precede the event edge by one tick, and clock pulses return to
idle one tick after it. Reset-only events do not create clock pulses.

## Open and arrange signals

Run **ZLang HDL: Open Waveform** or open an `.fst`/`.vcd` file. If another viewer
owns the file, choose **Reopen Editor With → ZLang HDL Waveform**. The
[simulation guide](reference/04-simulation-verification.md) explains how to generate traces.

The Design Browser shows the trace hierarchy. Expand scopes or filter by name.
Large signal lists load in pages of 200. Click **＋** beside a signal or
double-click its name to add a row. Each addition creates another independent
row, even when that signal is already displayed. A scope's **＋** adds its direct
signals; **Deep** includes descendants. A selection exceeding 4096 signal rows
(including copies) is rejected, so choose a smaller scope.
Ctrl/Cmd-click or Shift-click browser rows to select several and drag them into
the signal list or a group.

Click a displayed signal to make it active. Its name and waveform are
highlighted; the footer identifies it. Ctrl/Cmd-click or Shift-click selects
several for presentation changes. The last selected signal controls navigation
and measurements.

Drag pane boundaries to resize the browser, signal list, waveform, and Inspector.
**Inspector** collapses or reopens measurements; on narrow windows it opens over
the right side. **Group** and **Divider** add named sections. Drag rows to reorder
or nest groups. Click a group arrow to collapse it, double-click its name to
rename it, or use **×** to remove a row.

The Inspector offers a 16-color palette and a **Default** color reset. **Row
height** is a numeric field with up/down arrows, from 24 to 128 pixels. Color
and height apply to the selected rows; **Format** selects binary, hex, unsigned,
signed, or enum display for those rows. U/X/Z retain their distinct state colors.
Right-click the signal list or a waveform row for **Cut / Copy / Paste**, or use
**Ctrl/Cmd+X / C / V**. These operations copy selected rows with their appearance
settings, groups, and dividers through the system clipboard. Paste inserts after
the active row, inside an active group, or at the end when nothing is selected.
Cut removes rows only after a successful clipboard write. Shortcuts in text
fields continue to edit text normally. **Shift+F10** opens the row menu.
The VS Code Command Palette also offers **ZLang HDL: Cut Waveform Rows**,
**Copy Waveform Rows**, and **Paste Waveform Rows** while a waveform is active.
Copies have independent color, height, alias, and format, but share the underlying
trace data and queries. Removing one copy does not remove the others.
Pasting into another trace rebinds exact hierarchical names and widths; absent
signals become holders. Clipboard data contains no waveform samples. A holder's
saved diagram is retained only within the editor that already has its tiles.
Clipboard transfers are bounded to 1 MiB and the usual row/group limits.
**Alias** and **Mnemonics** edit the active row.
Enum display uses trace-provided labels when available. A custom mnemonic map
uses exact bit strings, for example `{"00":"Idle","01":"Header","10":"Payload"}`.

**Split＋** creates up to four time viewports. Click one to make it active.
With **Sync** enabled, panning or zooming applies the same interval to all
viewports. Otherwise each retains its own range. **Split－** removes the active
viewport.

## Navigate the trace

| Action | Result |
| --- | --- |
| Click / Shift-click | Place cursor A / cursor B |
| Left / Right, Change buttons | Previous / next active-signal change |
| Shift+Left / Shift+Right | Previous / next rising edge |
| Ctrl+Left / Ctrl+Right | Previous / next falling edge |
| Wheel | Zoom around the pointer |
| Shift+wheel | Pan horizontally |
| Left-button drag | Pan horizontally; a click without dragging places the cursor |
| Middle-button drag | Pan horizontally and scroll signal rows vertically |
| Drag the overview | Move the active time range |
| Fit, or double-click a viewport | Show the complete trace |
| A↔B | Zoom between cursors |

Waveforms update while panning, without waiting for the mouse button to be
released. The viewer fetches a bounded margin around the visible interval and
coalesces requests while moving. If a large jump outruns the loaded data, the
unfetched part stays empty until it arrives; the last value is not extended
into unqueried history.

Without an active signal, navigation searches all displayed signals. Previous
and Next exclude the current timestamp, including when a cursor lies between
integer timestamps. Search also supports exact numeric values, bit patterns
(`?` matches one bit), changes, rising/falling edges, and entry into X or U.
Numeric values accept decimal, `0x` hex, and `0b` binary. Rising/falling searches
apply to scalar signals.

**Snap** aligns a cursor with a nearby visible transition. The overview shows
activity across the complete trace; drag its viewport rectangle to move through
time. At dense zoom levels, activity buckets retain short pulses and U/X/Z
activity. Zoom in to see exact transitions and values.

## Values, bookmarks, and measurements

The signal list shows separate A/B values and a B−A delta. Cursor values are
queried independently of the visible range. The Inspector reports transition
counts, scalar rising/falling edges, high/low duration, and duty cycle. Duty is
unavailable when an interval contains unknown or missing values. Known buses
also have signed and unsigned deltas. U, X, and Z have distinct colors.

Native traces use `zlang_event_ticks`: two ticks represent one atomic simulation
event. The Inspector shows event deltas and no physical frequency. External
traces with a declared physical timescale also show seconds and `1 / Δt`.
Traces without a physical timescale remain in trace ticks.

**＋ Bookmark** records cursor A with a label. Click it to jump, double-click to
rename, or use **×** to delete it. **◀ M / M ▶** move between bookmarks. Sessions
support up to 256 bookmarks; colors can also be set in the session or through WCP.

## Portable sessions

Run **ZLang HDL: Save Waveform Session** to write a `.zvw.json` sidecar, and
**ZLang HDL: Load Waveform Session** to open its trace and restore the workspace.
The trace path is relative to the sidecar where possible, so they can move
together.

**Save Session** and **Load Session** are also available on the toolbar in both
saved and live views. A live sidecar is an explicit layout template with
`"source": {"kind": "live"}` instead of a `trace` path. Load it while a ZLang
debug session is active. It restores the workspace against that session's
recorded catalog, not a previous simulation's samples or state. Unrecorded or
incompatible signals stay unresolved; add them to the live trace to rebind exact
names and widths. The file does not execute simulation events or enable traces
implicitly. Saved-file sidecars retain their existing relative `trace` path.

The `zlang-waveform-session-v1` document stores order, groups, dividers,
presentation, pane sizes, viewports, synchronization, cursors, bookmarks,
selection, and search settings. It stores no samples. VS Code workspace state
provides automatic recovery separately.

When a catalog changes, rebinding requires exact hierarchical names and widths.
Missing, ambiguous, or incompatible signals remain visible as unresolved rows.
Saving again preserves those identities for later repair. Invalid sessions are
rejected before replacing the layout. Sidecars are bounded to 1 MiB, 4096
signal rows including copies, 8192 total rows, eight group levels, and four viewports.

## Live debugger traces

Start a [native debug session](native-debugging.md), then run **ZLang HDL: Open
Live Waveform**. The LIVE line marks the debugger event. **Follow Live** keeps
it visible while stepping adds data. Clicking in the waveform disables following
to inspect earlier data; the button enables it again.

An event file is optional for an interactive single-clock run. Follow the
[RuleAction manual-input walkthrough](native-debugging.md#debug-without-an-event-file)
to add a launch with `clock: "clk"`, `cycles: 20`, and no `events` field.
Press **F5** to stop on entry, then change `enable` under **Variables → Ports**
using **Set Value**. **F10** completes one clock event and adds a waveform
sample. An **F9** breakpoint on `when enable` or its state-update action stops
**F5 / Continue** after an enabled event and highlights the selected source
line. Editing an input alone does not advance the clock or add a trace sample.
The example starts from the declared register value, not a reset sequence;
use JSONL for explicit reset or repeatable input/clock sequences.

The [Python simulation example](native-debugging.md#python-simulation-without-an-event-file)
also needs no JSONL, but creates a separate native instance. This live panel
does not attach to it, and `.zhl` breakpoints do not stop that Python-driven
instance. Use the ZLang debug launch for source breakpoints and live waves, or
generate JSONL in Python and replay it in the debugger.

To trace a debugger value, right-click it in **Variables** or **Watch** and
choose **Add to Waveform**. Aggregate fields, indexed values and tagged-union
variants are supplied by the compiler's typed catalog. Interfaces appear as
groups with independently directed fields. **Type** format shows enum members,
signed/scaled values and explicit inactive/unknown-variant labels; raw radix
formats remain available. See the [typed tracing workflow](native-debugging.md#live-and-saved-waveforms)
for memory selection, limits and saved-file metadata boundaries.

The same Design Browser, duplicate rows, Cut/Copy/Paste, groups, 16-color
palette, row heights, radices, search, edge navigation, A/B cursors, bookmarks,
measurements and synchronized split viewports are available for live data.
**Add signals** opens the debugger's trace selector; the browser lists signals
already being recorded. Memory cells remain selectable through Variables.
**Export Trace** writes the recorded raw backing signals to FST or VCD while
the debug session is active. Open that file for inspection after ending the
session; nominal live struct/union metadata is not embedded in the export.

Live layouts autosave per program/top/profile in the VS Code workspace.
Closing and reopening the panel restores the layout, and restarting the same
session keeps groups, styles, split views and bookmarks while clearing the old
trace epoch. Cursor and bookmark times are clamped to the new available history.
Clearing the displayed rows stays empty until signals are explicitly added.
Changing to another debug design does not carry over the previous design's rows.

Moving cursors never changes simulator state. Step/Continue remain debugger
actions. Signals added during a run have a history-start notice. Restart to
record them from the beginning; restart clears the previous trace epoch.

Saved and live views request bounded render tiles, values, and measurements
from Rust. Complete sample histories are not transferred to JavaScript. Files
are limited to 1 GiB, render requests to 4096 signals and 8192 pixels, and
responses to a width-dependent budget. Each document's response cache has a
128 MiB accounting ceiling; Rust also limits its decoded-signal cache to
128 MiB. File indexes and active render buffers are separate from these caches.
Timestamps beyond JavaScript's exact integer range produce a diagnostic rather
than silent rounding.

## Troubleshooting

- **No waves in a saved file:** expand the Design Browser and click **＋**
  beside a signal, then **Fit**. An empty signal list does not display every
  signal automatically. If simulation replaced the file, wait for it to finish
  and click **Reload**.
- **No waves while debugging:** check that a `zlang-hdl` session is active and
  open **ZLang HDL: Open Live Waveform**, not a saved FST/VCD tab. Use **Add
  signals** or **Variables → Add to Waveform**, then execute an event. A signal
  first enabled for tracing during a run has no earlier history; restart for
  history from event zero. Adding another row for an already recorded signal
  does not discard its history.
- **No color palette:** click a displayed signal row, open **Inspector**, and
  look under **Selection → Color**. Selecting only a browser entry or group
  does not open signal presentation controls. On a narrow window, Inspector
  opens over the right side. Both saved and live views offer the 16 colors,
  **Default**, and the numeric **Row height** control.
- **Refresh does not advance the LIVE marker:** **Refresh** only rereads recorded
  history. Use debugger **F10 / Step Over** or **F5 / Continue** to execute
  events. Moving cursors and changing input values while paused do not step
  the simulator either.
- **The installed viewer looks older:** run `make editor-install` from the
  checkout you are using, run **Developer: Reload Window**, and reopen the
  waveform. Start a new debug session for the matching compiler and adapter.

## Waveform Control Protocol

WCP is disabled by default. Enable `zlang.waveform.wcp.enabled` in a trusted
workspace; port `0` chooses an available loopback port. **Output → ZLang HDL**
reports the address.

WCP v0 remains compatible. Negotiate v1 for `set_selection`, `set_cursors`,
`set_bookmarks`, `export_session`, `import_session`, `set_viewport_sync`, and
`get_measurements`. Unknown versions and commands return errors. WCP session
export/import transfers the document; filesystem Save/Load remains an explicit
editor action.

`set_selection` accepts row IDs for selecting an individual copy. Signal IDs or
hierarchical names select all displayed copies of that signal. `focus_item`
selects exactly one row by its row ID.

The workspace supports digital VCD and FST only. Analog lanes, GHW, expression
evaluation, protocol decoding, source cross-probing, driver tracing, and
simulator-state mutation are outside its supported surface.
