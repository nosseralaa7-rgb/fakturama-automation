# Fakturama Image-to-Cash Automation

Turns a single order image into a saved Order and a linked Invoice inside the
Fakturama desktop application, creating Debtor, Payment Method, VAT and Product
master data only when it cannot already be selected — with no hardcoded
coordinates.

- [DESIGN.md](DESIGN.md) — the architecture and the reasoning behind it
- [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) — annotated screenshots of a full
  run, and a screen recording of the same run

## Setup

Built and verified on macOS (Apple Silicon) against **Fakturama 2.2.0**.

### 1. Fakturama

The macOS build ships only as an install4j installer that requires root. To
install without it, unpack the payload directly — it is an LZMA stream wrapping
a ZIP whose entries use Windows path separators:

```bash
hdiutil attach -nobrowse Installer_Fakturama_macos-aarch64_2.2.0.dmg
python3 - <<'PY'
import lzma, zipfile, os, pathlib
src = "/Volumes/Fakturama_AARCH64/Fakturama Installationsprogramm.app/Contents/Resources/app/0.dat"
dest = pathlib.Path(os.path.expanduser("~/Applications"))
with open(src, "rb") as f, open("/tmp/payload.zip", "wb") as out:
    d = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
    while chunk := f.read(1 << 22):
        out.write(d.decompress(chunk))
z = zipfile.ZipFile("/tmp/payload.zip")
for info in z.infolist():
    name = info.filename.replace("\\", "/").removeprefix(".i4j_external_548").lstrip("/")
    if not name or info.is_dir() or name.endswith("/"):
        continue
    target = dest / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(z.read(info))
    if mode := info.external_attr >> 16:
        os.chmod(target, mode)
PY
xattr -r -d com.apple.quarantine ~/Applications/Fakturama2.app
codesign -f -s - --deep ~/Applications/Fakturama2.app
```

No JRE is bundled and the ARM build needs an ARM JVM. Unpack Temurin 17
(aarch64) into `~/Library/Java/JavaVirtualMachines`, then add these two lines to
`~/Applications/Fakturama2.app/Contents/Eclipse/Fakturama.ini` **before**
`-vmargs`:

```
-vm
/Users/<you>/Library/Java/JavaVirtualMachines/jdk-17.0.20+8/Contents/Home/bin/java
```

Launch it once and pick a working directory (it creates `Database/`,
`Templates/` there — point it somewhere deliberate, not your Desktop).

### 2. Toolchain

```bash
brew install tesseract cliclick     # OCR and synthetic mouse input
brew install ffmpeg                 # only needed to encode a recording
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Grant your terminal **Accessibility** and **Screen Recording** permission under
System Settings → Privacy & Security.

### 3. API key

Extraction needs one of these (in the environment or a `.env` file):

```bash
export OPENAI_API_KEY=...       # tried first
export ANTHROPIC_API_KEY=...    # fallback; an `ant auth login` profile also works
```

Neither is needed to run the test suite.

## Running

```bash
.venv/bin/python scripts/run.py assets/sample_order.png                # full flow
.venv/bin/python scripts/run.py assets/sample_order.png --extract-only # no UI
.venv/bin/python -m pytest tests/ -q                                   # tests
```

The run needs the foreground: it activates Fakturama and drives it with synthetic
input, so leave the machine alone while it works. Every step writes a numbered
screenshot and a structured log line to `runs/<timestamp>/`.

Exit codes: `0` success, `1` unexpected failure, `2` **manual review required**
(with the stage, the reason and the candidates it saw), `3` the idempotency
assertion below failed.

Also useful:

```bash
# second run over the same image: everything must be selected, nothing created
.venv/bin/python scripts/run.py assets/sample_order.png --expect-selection-only

# start from an empty database (destroys the working directory)
.venv/bin/python scripts/reset_fakturama.py

# re-run single stages against an already-open Order, without re-extracting
.venv/bin/python scripts/dev_stage.py order.json --stages items save invoice

# record the Fakturama window while a run drives it
.venv/bin/python scripts/record.py --out docs/run.mp4 --stop-file /tmp/stop-rec

# inspect what the accessibility tree exposes
.venv/bin/python scripts/ax_dump.py Fakturama --depth 12
```

## Layout

```
src/driver/     base.py (UIDriver interface) · ax_driver.py (accessibility) · vision_driver.py (OCR + pixels)
src/extraction/ models.py (Decimal-typed schema) · validate.py (arithmetic self-check) · extract.py
src/flow/       app.py (Fakturama vocabulary) · dialogs.py · reading.py (list views) · expect.py (screen
                assertions) · items_grid.py (Items-table geometry)
                order.py · debtor.py · product.py · invoice.py  (the five stages)
src/            llm.py (OpenAI → Anthropic) · runlog.py · manual_review.py
scripts/        run.py (entry point) · reset_fakturama.py · dev_stage.py · record.py · ax_dump.py
                render_sample_order.py
docs/           WALKTHROUGH.md · screenshots/ · run.mp4 · run-log.json
```

## Status

**The full flow runs end to end from an empty database**, and a second run over
the same image creates nothing. One image in, a saved Order and a linked paid
Invoice out:

```
Documents
  INV000001   Aug 17, 2026   Nordlicht Handels ...   PO-2026-0412   paid   EGP559.42
  PO000001    Mar 12, 2026   Nordlicht Handels ...   PO-2026-0412   open   EGP559.42
```

Reproduce with `python scripts/reset_fakturama.py && python scripts/run.py
assets/sample_order.png`. The evidence for the run in `docs/` is
[docs/run-log.json](docs/run-log.json) — the complete transcript — plus the
annotated screenshots and the recording.

### What the run actually verifies

Nothing is assumed to have worked because a click returned:

- extraction reconciles with the document's own line and grand totals **before**
  the UI is touched
- the price mode is `Net` and the VAT mode is still `With VAT`
- the payment method, VAT rate, Debtor and both Products are created and then
  **selected back from the still-open Order** — that selection is the proof each
  one saved
- a VAT rate is only reused when its name, its value *and* its `S (Standard
  rate)` E-Invoice code all match; the code is read from the record itself,
  because the list does not show it
- each item line shows the extracted VAT rate and the total Fakturama computed
  from quantity × unit net × (1 − discount): `EGP300.00` and `EGP170.10`
- order-level Discount is `0%` and shipping is `Free of shipping costs`
- the Order's totals match the document: 470.10 net / 89.32 VAT / 559.42 gross
- each document's **whole row** in `Data > Documents` carries the right number,
  date, reference, state and total together — the Order `open`, the Invoice
  `paid`
- the Invoice inherited the reference, Order Date, VAT mode, both item lines and
  the total, carries the right payment method, and is paid for the full amount on
  the extracted payment date
- a second run selects every master record and creates none
  (`--expect-selection-only`, exit 3 if not)

## Where this departs from the letter of the specification

Each of these is a route difference, not an outcome difference, and each is
commented at the point in the code where it happens.

| Specification | What this does instead | Why |
|---|---|---|
| 2.10 create the payment method from inside the open Debtor editor | Creates it before opening that editor | This build populates the Payment combo when the editor opens and never refreshes it, so a method created afterwards is simply not offered |
| 2.10 "Open Payment" tab | Uses the Miscellaneous tab | 2.2.0 has no Payment tab; payment, Discount and Net/Gross all live on Miscellaneous |
| 2.5 / 2.10.2 green + and "New Contact" controls | Uses the `New` menu | The panel's add icon sits ~20 points from the delete icon; the menu bar is fully exposed to accessibility and unambiguous |
| 2.3 / 3.3 "select it and click OK" | Double-clicks the row | A single click does not move these SWT tables' selection, so OK closed the dialog having selected nothing — and left the address empty |
| 3.3 search the exact SKU | Scans the list unfiltered | Typing into that dialog's search box — or setting it through accessibility — dismisses the dialog. Limitation: a catalogue long enough to scroll would need paging |
| 3.13–3.17 complete each line before selecting the next Product | Resolves all Products, then completes all lines | Adding a Product resets values already typed into the rows above it |
| 3.16 confirm the line Price cell | Confirms it from the row after moving focus out of the table | A selected row renders white-on-blue and OCR reads nothing from it |

## If I had 3 more hours

1. **Make the manual-review stop resumable.** It currently exits; it should
   persist the extracted `OrderData` and the stage reached, so an operator can
   fix the ambiguity and resume rather than re-run from the image.
2. **Move the label patterns into a config file.** They are inline in the stage
   modules. In YAML they become the single place to re-ground when binding the
   same stages to Windows/UIA — which is also the honest way to make the "it
   ports" claim in DESIGN.md concrete.
3. **One retry policy instead of several.** Retries are currently added where a
   read proved flaky — locating a label, matching a list row, reading the payment
   amount — each with its own loop. They belong in one place in the driver, with
   backoff and a budget, so that "how many times do we look before we believe the
   screen" is a single decision rather than seven.
4. **An extraction test corpus** — several order images with known-correct
   expected output, including deliberately broken ones, to prove the reconciler
   catches what it claims to. Today the reconciler is unit-tested against
   fixtures, but only one real image has been through the model.
5. **Page the Product selector's list** so the SKU check survives a catalogue
   bigger than one screen, and take the same look at the address selector.
6. **A dry-run mode** that resolves and verifies everything but never saves,
   which would make it safe to point at a production database.
