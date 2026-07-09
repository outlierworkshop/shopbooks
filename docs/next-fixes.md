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

### 2 — Route plumbing: connection dependency + `safe_redirect` · [#73](https://github.com/outlierworkshop/shopbooks/issues/73)
- Add to **`webutil.py`**: `get_con()` (a FastAPI `Depends` generator that yields a connection and
  closes it in `finally`) and `safe_redirect(back, fallback="/", msg=None, err=None)` (the
  `startswith("/")` guard + quoted `msg`/`err`). Handlers still call `con.commit()` explicitly — that's
  intentional, not boilerplate.
- Migrate the ~145 hand-rolled `db.connect()/try/finally` + copy-pasted redirect guards **one
  `routes_*` module per commit**. Do NOT change commit placement/semantics — plumbing swap only.
  Error-path tests (`test_bulk_actions`, `test_period_lock`) must still pass.
- Payoff: ~−300 lines, one definition of how a route gets a connection. Verify: suite green after each module.

### 3 — Logging baseline (observability) · [#74](https://github.com/outlierworkshop/shopbooks/issues/74)
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
