# Fakturama Image-to-Cash — Design

## Objective

Turn one order image into a saved, verified Order and a linked Invoice inside
Fakturama, creating Debtor, Payment Method, VAT and Product master data only
when it does not already exist — without hardcoded coordinates.

## Shape of the problem

Two hard parts, and they are unrelated to each other:

1. **Reading the document.** Unstructured pixels to structured, *trustworthy*
   numbers. The failure mode is a plausible misread.
2. **Driving the application.** Finding and operating controls in a desktop app
   whose layout is not known in advance. The failure mode is acting on the
   wrong control, or believing an action succeeded when it did not.

The design keeps them apart. Extraction never touches the UI; the UI layer never
interprets the source document. Between them sits a validated data object.

```
order image ──▶ extraction ──▶ OrderData ──▶ flow (5 stages) ──▶ driver ──▶ Fakturama
                    │            (validated)        │              │
               vision model                   state machine    AX tree
               + arithmetic                   verify each      + OCR/vision
               self-check                     step             grounding
```

## Grounding: how controls are found

The rule is that a control is addressed by **identity**, never by a coordinate
that was written down in advance. Two mechanisms provide identity, and the
application forces the use of both.

Fakturama is an Eclipse RCP/SWT application. Measured against Fakturama 2.2.0 on
macOS, its accessibility tree is **split**:

| Reachable via accessibility | Invisible to accessibility |
|---|---|
| Toolbar buttons (`Order`, `Invoice`, `Save`, `Product`, `Contact`) | The entire document editor body |
| Left Navigation View (`Documents`, `Debtors`, `VATs`, `terms of payment`, `New product`, `New Contact`) | `Cust.Ref`, `Date`, the price-mode combo |
| All modal dialogs — buttons, checkboxes, text fields | The address selector icons, the whole Items table |
| Editor **tab groups** — title, bounds, and which tab is active | Everything inside those tab groups |

A New Order editor with every field populated still exposes only ~47 nodes.
So the design is a **two-layer driver behind one interface**:

- **Structural layer (`AXDriver`).** Finds elements by role and title, presses
  them with `AXPress`, sets text with `AXValue`. No coordinates at all. This is
  the direct analogue of Microsoft UI Automation's Invoke and Value patterns.
- **Pixel layer (`VisionDriver`).** For the editor body. Grounding is layered,
  cheapest and most precise first:
  1. **OCR anchoring.** Tesseract returns exact pixel boxes for visible words.
     Labels in this UI sit immediately left of the control they name, so a field
     is addressed as *"the input to the right of `Cust.Ref.`"*. The coordinate is
     computed from the current frame, so the window may be moved or resized
     between steps.
  2. **Vision-model grounding** for controls with no adjacent text (icons).

  What makes this layer work is not the OCR call, it is **how the image is cut up
  before it**. Recognition on this UI is dominated by crop geometry, and the same
  screen yields different words depending on it:

  | Shape of the crop | What happens | What to do about it |
  |---|---|---|
  | A whole 1500×900 window | Entire bands of the form are silently dropped | Crop to the panel, then upscale |
  | A tall panel in one pass | Individual words go missing, unpredictably | Read it in half-overlapping horizontal strips |
  | A wide, short row (a list row, a table header) | Words vanish from the *middle* of the row | Also read it in vertical slices and merge — the two readers' misses do not coincide |
  | A single-line field | Words drop out of the middle again | Tell Tesseract it is one line (PSM 7) |
  | A boundary through a row's glyphs | The row is not lost, it is **corrupted** — `24.00 NL-4021 Edelstahl` read back as `Lt )` | Never cut a row; include a header instead |
  | A value that has to be *found*, not just read | One row's number is dropped pass after pass while its neighbours read perfectly | Read that column on its own, in short bands |
  | A field holding keyboard focus | Drawn highlighted with a caret in it; `559.42` came back as `59.42` | Move focus off before reading it back |

  Two consequences run through the whole codebase. Nothing is verified from a
  single pass, and a *miss* is retried while an *ambiguity* is not — a miss is
  usually the reader, an ambiguity is usually the screen. And a value that
  identifies something is never trusted to row segmentation: the row is found by
  anchoring on that value and then reading its own line, because clustering words
  into rows is exactly as unreliable as the baselines it depends on.

The seam between them is the interesting part: **accessibility supplies the
bounds, OCR reads what is inside them.** The tree still knows the layout
containers even where it cannot see the controls, so the editor's region comes
from `AXTabGroup`, and OCR is cropped to it. That both improves recognition
accuracy and disambiguates labels that repeat across panels — `Cust.Ref.`
appears in the editor *and* as a Documents column header.

### Why this ports

The flow stages are written against a `UIDriver` interface — `find`, `press`,
`set_value`, `screenshot`, `wait_until` — and never name a mechanism. On Windows
the same interface binds to **UIA**: `press` to the Invoke pattern, `set_value`
to the Value pattern, `find` to a property-condition tree search. The stage code
does not change. What does not port is the *selector content*: a Win32 SWT
accessibility tree exposes different roles and names than a Cocoa one, so
selectors must be re-grounded against the target platform. That is a real cost
and it is better to state it than to imply a clean recompile.

## Extraction

A vision model returns JSON against a strict schema; money is parsed into
`Decimal` (never float), and dates accept the German `DD.MM.YYYY` on the source.

The important part is not the model call — it is what happens next. The
extraction is **reconciled against the document's own totals** before the UI is
touched at all:

- each line: `quantity × unit_net × (1 − discount%)` must equal the printed line total
- the sum of line nets must equal the printed net total
- per-line VAT must sum to the printed VAT
- net + VAT must equal the printed gross
- `PAID` and a payment date must agree with each other

A misread digit almost always breaks one of these identities. If any check
fails, the run stops **before** a single record exists. This is the cheapest
place in the whole system to catch an error: nothing has been created, so there
is nothing to unwind.

## Verification

Every state change is confirmed by reading state back, never by sleeping:

| Action | How success is proven |
|---|---|
| Editor opened | The active tab title matches, read from the accessibility tree |
| Save | Eclipse's dirty marker (`*New Order` → `New Order`) disappears — a structural signal, not a pixel one |
| Master record created | It can be **selected from the still-open Order** — the specification's own test, and a stronger one than reading the save dialog |
| Item line correct | The line's own VAT and total are read back off the row, and Fakturama's recomputed totals are matched against the document — the line total is a function of quantity, price and discount, so agreement proves all three at once |
| Whole run | Each document's **whole row** in `Data > Documents` carries its number, date, reference, state and total together |
| Idempotency | A second run over the same image must create nothing — the run log's creation events are the assertion |

Reading values back through OCR is unavoidable — the editor body is invisible to
accessibility — but Tesseract misreads this UI's field font (`PO-2026-0412` came
back once as `(PO-2026-0419`), so equality is the wrong comparison. Every
expectation is instead a **bounded tolerant pattern**: an amount matches
`559.42`, `559,42` or `EGP559.42` but not the `559.42` inside `1559.42`; a
document number tolerates the O/0 confusion this font invites; a percentage
matches both `19%` and the `19.00 %` the lists print. Those patterns are pure
functions and unit-tested in both directions, because a pattern that is too loose
passes a wrong document and one that is too strict fails a correct run — and here
the second costs as much as the first.

Assertions are also **row-scoped** rather than region-scoped wherever a list is
involved. The Documents panel holds the Order and its Invoice, so `open`, `paid`
and the total are all present in it no matter which document carries which; only
"one row shows all of these together" verifies anything.

## Failure handling

The specification repeatedly says to stop rather than guess, and the code takes
that literally. `find_one` raises on ambiguity instead of picking the first
match; every ambiguous branch raises `ManualReviewRequired` carrying the stage,
the reason, and the candidates it saw. The entry point turns that into a
non-zero exit, a written report, and a screenshot of the exact screen.

This is a deliberate bias. In an accounting system a wrong Debtor or a duplicated
Product is silent, persistent, and discovered late — much worse than a run that
halts and asks.

Every step also writes a numbered screenshot and a structured log line to
`runs/<timestamp>/`, which is both the debugging trail and the evidence that the
run did what it claims.

## Tradeoffs

*(Author's note — this section is where I'd expect the most discussion, and I've
kept it as the honest version rather than the flattering one.)*

- **Two grounding layers instead of one.** More moving parts and a genuinely
  harder failure surface than a pure-tree approach — but a pure-tree approach
  cannot reach the editor body at all on this application, and a pure-pixel
  approach throws away reliable identity where it exists.
- **OCR before the vision model.** Tesseract is free, fast and returns exact
  boxes; the model is slower and costs per call. Using the model only where text
  anchoring cannot reach keeps a long run affordable. The cost is a second
  failure mode to reason about — OCR recall varies between runs, which is why
  several page-segmentation modes are merged.
- **Stop-on-ambiguity over best-effort.** Fewer completed runs, no bad data.
- **Verifying through the application's own state** (dirty marker, recomputed
  totals, selector availability) rather than a separate database read: it tests
  what the user would test, and needs no knowledge of the storage layer — but it
  is only as trustworthy as the UI's own consistency.
- **Serial execution.** Nothing runs in parallel; a desktop UI has one keyboard
  focus, so concurrency would be a correctness bug, not a speedup.

## Known limits

- **Platform.** Built and verified on macOS against the Fakturama 2.2.0 ARM
  build. The driver interface is the portability seam and the Windows binding
  would be UIA, but the accessibility selectors would need re-grounding there.
- **Shared desktop.** Synthetic input goes to whichever application owns the
  foreground, so a run needs the machine. The driver asserts focus before every
  click and discards a screenshot if focus was lost mid-capture, which makes
  interference safe rather than silent — but not harmless.
- **macOS Stage Manager** hides non-focused windows, which breaks
  `screencapture -l`; capture therefore goes through `CGWindowListCreateImage` on
  the window's own backing store, which is also why an overlapping window cannot
  corrupt a capture — an earlier screen-crop approach once photographed a browser
  sitting at Fakturama's coordinates.
- **Catalogue size.** The Product selector's search box dismisses the dialog when
  driven, so its list is scanned unfiltered; a catalogue long enough to scroll
  would need paging, which is not implemented.
