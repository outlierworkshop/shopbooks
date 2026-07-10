# Next fixes — working queue

Short, actionable queue of the fixes lined up next. Full detail + discussion lives in the linked
GitHub issues; this file is the at-a-glance order of work so either machine (or a future session) can
pick up without digging. Per CLAUDE.md, **never run the app/tests against real books** — set
`SHOPBOOKS_DATA_DIR` to a temp dir. Suite: `python run_tests.py` (must stay green).

## Code-quality review (2026-07-09) — remaining items
The 5-item review's first two are done: **#1** failing-test harness → PR #70, **#2** `app.py` carve into
`routes_*.py` → PR #71 (both merged). Remaining:

### 1 — ✅ DONE (2026-07-09) — Dedupe line-item JS + fold dashboard CSS · [#72](https://github.com/outlierworkshop/shopbooks/issues/72)
Shipped: `static/line-items.js` replaces the triplicated editor JS; dashboard `<style>` moved into
`style.css`; rogue `var(--text)` → `var(--ink)` everywhere. (Optional inline-style utility pass skipped
per the issue.) The two items below remain.
- Extract one **`static/line-items.js`** (load in `base.html` with `?v={{ static_v() }}`) to replace the
  copy-pasted `standardItems` / `onItemSelect()` / `escapeHtml()` / `addRow()` / `syncTax()` in
  `templates/invoice_new.html`, `invoice_edit.html`, `estimate_new.html`. Templates keep only the
  Jinja `standardItems` JSON bootstrap + the table markup. `addRow()` should read columns from the
  header row (invoice_edit has an extra delete-button column) rather than hardcoding.
- Move `dashboard.html`'s ~600-line inline `<style>` into `style.css` (`/* dashboard widgets */`),
  converting rogue vars to system tokens (`--text` → `--ink`, etc.). **Verify light AND dark** (nav toggle).
- Optional (skippable): replace heavy inline `style="…"` on `items.html` / `customer_detail.html` /
  `invoice_view.html` with small utility classes.
- Verify: suite green; add rows / pick catalog items / tax-checkbox auto-sets on invoice-new/edit &
  estimate-new; dashboard renders in both themes.

### 2 — ⏳ IN PROGRESS — Route plumbing: connection dependency + `safe_redirect` · [#73](https://github.com/outlierworkshop/shopbooks/issues/73)
**Pattern established (2026-07-09, commit 544f6f0):** `webutil.get_con()` + `webutil.safe_redirect()`
exist and `routes_entries.py` is migrated as the reference example — copy its style. Notes learned:
- `db.connect()` now uses `check_same_thread=False` (FastAPI runs sync deps + handlers on different
  threads); connections stay short-lived/sequential, so this is safe. Already committed.
- Add `con=Depends(get_con)` as the LAST route param; drop the `db.connect()/try/finally` wrapper but
  KEEP `try/except ValueError` blocks and the explicit `con.commit()` calls exactly where they were.
- `safe_redirect(back)` for plain guarded redirects; `safe_redirect(back, msg=…)`/`err=…` replaces the
  quote() + sep math. Watch for handlers that redirect to a fixed path (pass it as `back`).
- Migrate **one module per commit**, `python run_tests.py` green after each. Plumbing swap only — do
  not change commit placement/semantics.

**Progress (2026-07-10, commit ccc6999):** routes_dashboard, routes_feeds, routes_items done too — 4 of
16 modules migrated (~12 of ~145 connects). Free fix found: routes_items.py built redirect URLs with
raw f-string interpolation (unquoted err/msg) — a real query-string-corruption bug, now fixed by
`safe_redirect`'s `quote()`. Worth re-grepping other modules for the same `f"...?err={e}"` pattern
while migrating them.

**Remaining modules (by size):** routes_invoices (23 connects) · routes_receipts (16) ·
routes_settings (15) · routes_time (10) · routes_estimates (10) · routes_customers (10) ·
routes_taxes (7) · routes_reports (7) · routes_recurring (7) · routes_reconcile (6) ·
routes_migrate (6) · routes_review (5).

### 3 — ✅ DONE (2026-07-09) — Logging baseline (observability) · [#74](https://github.com/outlierworkshop/shopbooks/issues/74)
Shipped: `logutil.py` (rotating `<datadir>/logs/shopbooks.log`, isolated via `db.DATA`) + `log.warning`
before the silent swallows in ai.py/staging.py/routes_receipts.py/watcher.py/sync.py/routes_review.py/
chat.py. `test_logutil.py` added. Optional Settings log-viewer left for later. Original spec below.
- Tiny **`logutil.py`**: stdlib `logging` + `RotatingFileHandler` → `<datadir>/logs/shopbooks.log`
  (~1MB × 3), INFO, plus console. **Resolve the data dir exactly like `db.py`** (`SHOPBOOKS_DATA_DIR`
  first) so tests never write logs into the real data dir.
- Add `log.warning("<what failed>: %s", e)` before the ~47 broad `except Exception` swallows (grep
  `staging.py`, `routes_*.py`, `ai.py`, `feeds.py`, `sync.py`). **Observability only — do not change any
  fallback behavior.** `ai.py`'s "return None, never raise" contract stays; it just logs why.
- Optional (separate commit): surface the last few log lines on the Settings page.
- Verify: suite green; with `SHOPBOOKS_DATA_DIR` set the log lands in the temp dir; an AI path with no
  key writes a warning line.

## Bigger bets (separate track, later)
- Mobile receipt capture — [#42](https://github.com/outlierworkshop/shopbooks/issues/42)
- Online invoice payments — [#41](https://github.com/outlierworkshop/shopbooks/issues/41)

## Also noticed (unfiled loose ends — confirm before doing)
- `base.html` loads `/static/resizable.js?v=1.1` with a **hardcoded** cache token (not
  `?v={{ static_v() }}` like the others), and it looks redundant with `resize.js` — reconcile the two.
- Reactivate any hidden seed accounts you actually use (e.g. **Contract Labor**, account id 12 — hidden,
  no transactions) rather than recreating them.
