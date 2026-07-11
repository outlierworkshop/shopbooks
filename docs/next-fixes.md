# Next fixes — working queue

Short, actionable queue of the fixes lined up next. Full detail + discussion lives in the linked
GitHub issues; this file is the at-a-glance order of work so either machine (or a future session) can
pick up without digging. Per CLAUDE.md, **never run the app/tests against real books** — set
`SHOPBOOKS_DATA_DIR` to a temp dir. Suite: `python run_tests.py` (must stay green).

## Code-quality review (2026-07-09) — ALL 5 ITEMS DONE
**#1** failing-test harness → PR #70, **#2** `app.py` carve into `routes_*.py` → PR #71, **#72**
(line-item JS + dashboard CSS), **#73** (route plumbing), **#74** (logging baseline) — all merged.
Kept below for the implementation notes/lessons; nothing left to do here.

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

### 2 — ✅ DONE (2026-07-10) — Route plumbing: connection dependency + `safe_redirect` · [#73](https://github.com/outlierworkshop/shopbooks/issues/73)
Shipped across 7 commits (544f6f0 → ed4d8d1): `webutil.get_con()` (a `Depends` connection-per-request
generator) and `webutil.safe_redirect(back, fallback, msg=, err=)` replace ~145 hand-rolled
`db.connect()/try/finally` blocks and copy-pasted redirect-quoting logic across **all 16 of 16**
`routes_*.py` modules. Handlers kept their explicit `con.commit()` calls and `try/except` structure —
plumbing swap only, no behavior changes intended. `db.connect()` gained `check_same_thread=False`
(FastAPI runs sync `Depends` generators and handlers on different threads; connections stay short-lived
and sequential, so this is safe — `test_splits.py` caught the cross-thread error before this fix).

**Free fixes found along the way** (real latent bugs the migration incidentally corrected):
- `routes_entries.py`: `entry_delete` redirected to an unguarded `back` on success (open-redirect); `entry_edit` didn't quote its error message.
- `routes_items.py`: built redirect URLs with raw unquoted f-string interpolation (`f"/items?err={e}"`) — query-string corruption if the message had `&`/`#`.
- `routes_invoices.py`: `invoice_email`'s error redirect was entirely unquoted with a literal space in it — a malformed URL on any SMTP failure.
- `routes_settings.py`: `backup_restore` held a dead, unused `db.connect()`/`close()` pair — removed.
- Caught during migration (not shipped): dropping `import db` from `routes_review.py` broke `db.DOCS`
  at runtime — invisible to `import app`/compilation, only surfaced on an actual statement upload.
  Found by running `pyflakes` across all 16 migrated modules + `webutil.py` (only other finding: one
  pre-existing, unrelated unused-variable warning in `routes_settings.py`).
- `routes_migrate.py` and `routes_reconcile.py`/`routes_review.py`'s internal helpers kept their own
  already-correct redirect logic rather than force-fitting `safe_redirect` everywhere — a deliberate
  minimal-footprint call, not every route needs the shared helper if it already does the right thing.

**Verification method that worked well:** compile-check → `import app` → `pyflakes` (catches
runtime-only NameErrors that compilation/import miss) → full suite (`python run_tests.py`, 57/57 every
time) → live GET smoke test of every touched route. **Caution:** a POST smoke test against the real
running server mutates the real books — we once added+deleted two throwaway test accounts to exercise
an IntegrityError path; cleaned up and confirmed via `/sync/now` that the round-trip left no drift.
Prefer GET-only smoke tests; if a POST must be tested live, clean up immediately and verify via sync.

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
- ✅ DONE (2026-07-10) — `resizable.js` vs `resize.js`: turned out to be a genuine live bug, not just
  redundancy. `resizable.js` was leftover pre-`resize.js` code (superseded, never removed) that ran
  unconditionally on **every** table on every page: it added its own resize handles alongside
  `resize.js`'s on any `.resizable` table (confirmed live on Review — every `<th>` had two overlapping
  handles), and because its handle never calls `stopPropagation()` on click, a plain click (no drag)
  on it bubbled up and mis-triggered `sort.js`'s column sort (reproduced via dispatchEvent). It also
  forced `table.style.width = "100%"` after `resize.js` deliberately set `width: auto`, undermining the
  persisted per-column-width design, and applied unwanted resize handles + auto-wrapping to plain
  `sortable` tables that never opted in (e.g. `/register/{id}`). Deleted `static/resizable.js` and its
  `<script>` tag in `base.html`; `resize.js` (opt-in via `class="resizable"`, localStorage-persisted,
  sort-safe) is the one real system and needed no changes.
- Reactivate any hidden seed accounts you actually use (e.g. **Contract Labor**, account id 12 — hidden,
  no transactions) rather than recreating them.
