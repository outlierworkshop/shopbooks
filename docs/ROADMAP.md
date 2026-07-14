# ShopBooks Roadmap & Changelog

This file is the project's shared memory across maintainers (human and AI).
**When you ship a change, add a changelog entry.** When you start a roadmap item, note it.
Keep entries short — what changed and why, not how.

## Vision

An "all-around office manager" for a one-person business — statements in, clean books and
tax packages out, with AI doing the tedious parts and the human approving. **As of July 2026,
that vision is built.** What exists today:

- **Bookkeeping core** — double-entry ledger, statement import (AI or regex), human-confirmed
  Review, receipts (upload/folder/Amazon, auto-match, missing-receipt report), rules + history +
  AI categorization, QuickBooks migration, two-machine cloud sync.
- **Trustworthy books** — per-transaction reconciliation clearing, statement balance checks,
  year-end close / period lock (a filed year can't change by accident).
- **Get-paid loop** — estimates → invoices (partial payments, multi-deposit matching, editing),
  AR aging + overdue reminder emails, customer credits / credit memos / overpayment handling.
- **Proactive office manager** — a "what needs me today" dashboard briefing, 30–180-day cash-flow
  forecast, recurring bills (with auto-detection from history), Schedule C mapping, quarterly
  estimated-tax calculation + payment tracking + due-date reminders, per-year tax package ZIP.
- **AI layer (optional everywhere)** — the deterministic `insights.py` book-query foundation, the
  Opus Assistant (tool-grounded chat: how-to, tax strategy, analysis on real numbers), narration
  on Insights/briefing/forecast. Numbers always come from the ledger; the model never computes.
- Plus invoicing/email/PDF, job costing + time tracking, mileage, reports.

**Deliberately parked** (issues #41–#42): online invoice payments and mobile receipt capture — each
adds external dependencies that cut against the constraints below; revisit only if a real pain point
emerges. (Bank feeds, #43, was un-parked and shipped 2026-07-02 once SimpleFIN proved a local-first-
compatible path.) Future work is now refinement driven by daily use, not a checklist.

Guiding constraints (unchanged) live in `ARCHITECTURE.md` §Design goals — local-first, AI-optional,
boring tech, built for exactly one user.

## Changelog
### 2026-07-14 — Company logo: SVG (vector) support
- The logo upload now accepts **SVG** in addition to PNG/JPG/GIF. On invoices the SVG is rendered as
  true vector via fpdf2 (crisp at any size; `pdf.image` handles SVG, `<text>`-outlining recommended
  and warned about on upload). Because email clients can't render SVG and there's no server-side
  rasterizer without a heavy native dep, the Settings upload **rasterizes the SVG to a PNG in the
  browser** (canvas) and stores it as a companion (`company_logo_raster`) used only for emails —
  one upload brands both. Raster uploads still serve invoice + email directly. Falls back to a
  plain SVG-only upload if the browser can't rasterize (email then omits the logo). New helper
  `db.company_logo_raster_path`; `test_company_logo.py` covers the SVG + companion paths.

### 2026-07-13 — Company logo: upload once, shows on invoices + emails
- New **Settings → Company logo**: upload a PNG/JPG/GIF (validated, ≤3 MB, stored in the data dir)
  with a live preview and Remove. `render_pdf` puts the logo back in the invoice header (top-left,
  height-constrained so any aspect ratio fits; a bad/unsupported image is caught and skipped so it
  can never break invoice generation). Invoice/reminder emails are now **multipart** — a branded
  HTML body with the logo inline (`cid:`), plus the existing plain-text fallback and PDF attachment.
  `db.company_logo_path` is the single source of truth. This replaces the hardcoded
  `static/logo.png` (Outlier Workshop wordmark) that used to be baked into every invoice, so any SB
  user can brand their own. Routes: `GET`/`POST /settings/logo`, `POST /settings/logo/remove`.
  `test_company_logo.py` covers upload/serve/validation/embed/email; `test_email.py` updated for the
  multipart body.

### 2026-07-13 — Invoice/estimate/credit-memo PDF: clean-minimal redesign + punctuation fix
- Rebuilt `invoicing.render_pdf` from a generic template into a **clean-minimal** layout: sans
  throughout, a letter-spaced `INVOICE`/`ESTIMATE`/`CREDIT MEMO` label with a large document number,
  hairline table rules (no filled rows), a right-aligned totals block with a ruled **Total due**, and
  an anchored footer — so a high-ticket custom-instrument invoice no longer reads like a utility bill.
  Every money branch is preserved (subtotal, sales tax, payments received, credits applied,
  credit-memo, estimate, remaining balance due). Chosen from three rendered directions (Refined /
  Minimal / Bold).
- Fixed a real bug in `_latin`: common non-latin-1 punctuation (em/en dash, curly quotes, ellipsis,
  bullet, arrow, ™) now transliterates to ASCII instead of rendering as **"?"** under fpdf2's built-in
  fonts — e.g. "mandolin — Model A" printed as "mandolin ? Model A".
- The minimal look is intentionally **logo-less** (matches the chosen direction); re-adding the
  letterhead logo is a small change. `test_invoice_pdf.py` covers the punctuation fix and that all
  three document kinds render.

### 2026-07-13 — Bill a not-yet-a-customer on new invoices/estimates
- The new-invoice and new-estimate forms required picking an *existing* customer (and hid the whole
  form until you'd added one). Now the customer `<select>` includes a **"— New customer (enter
  below) —"** option plus **name + optional email** fields; picking an existing customer hides them
  (small `toggleNewCustomer` script). On submit, `invoicing.resolve_customer_id(con, form)` uses the
  picked customer or creates one inline from the typed name/email. Wired into `invoice_create` and
  `estimate_create` (edit routes unchanged). `test_new_customer.py` covers both flows, the
  inline-error path, and that the existing-customer path still works.

### 2026-07-13 — Dark mode: fix hardcoded-light fields that hid their text
- Several panels used inline hardcoded light backgrounds (`#fff6f6`, `#fdfdfb`, `#fff`, `#f6f8f6`,
  `#fdf3e4`, chat bubbles, …) with no explicit text color, so in dark mode the theme's light text
  landed on a light box and became unreadable — most visibly the Settings → Data sync **conflict
  card** (cloud-vs-local choice). Swapped them for the existing theme variables (`--bad-soft`,
  `--card-2`, `--bg-2`, `--warn-soft`, `--good-soft`, `--line`) so they adapt. Touched
  `settings.html`, the receipt/invoice/review **match drawers**, `insights.html`, `taxes.html`,
  and `chat.html`. Verified the vars resolve to dark values in dark mode.

### 2026-07-13 — In-app Help menu (renders the guides; fixes private-repo links)
- Added a **Help** nav dropdown and a `/help` hub that renders the project guides inside ShopBooks —
  User Guide, Email setup, and Automatic mileage — instead of linking to the (private) GitHub repo,
  which dead-ended for the owner. Hub has quick links to the monthly routine, invoicing, deductions,
  reports, and the AI assistant; each guide page has a topics sidebar.
- `helpdocs.py`: a small dependency-free Markdown-subset renderer (headings, wrapped bullet/numbered
  lists, GitHub pipe tables, fenced + inline code, bold/italic, links; source HTML escaped) serving a
  whitelist of docs. `routes_help.py` (`/help`, `/help/{slug}`), `templates/help.html`, help CSS.
- Fixed the three private-GitHub `blob/main/docs/...` links (Settings ×2, Mileage ×1) to point at the
  new in-app `/help/email` and `/help/mileage` pages. `test_help.py` covers the renderer subset, HTML
  escaping, every whitelisted doc rendering artifact-free, the routes, and that the private links are
  gone. Suite 64/64.

### 2026-07-13 — "Send test email" button + Google Workspace email setup guide
- Invoice email over SMTP already worked; the friction was that Google Workspace/Gmail needs an
  **App Password** (plain passwords are rejected) that only appears after 2-Step Verification is on,
  and there was no way to test config short of emailing a real invoice.
- Added a **Send test email** button on Settings (mirrors the Ollama test button): sends a
  self-addressed message via the saved SMTP settings and reports success or a plain-language error.
  `invoicing._smtp_send` (factored out, reused by `send_invoice_email`), `send_test_email`, and
  `explain_smtp_error` (535/auth → App Password guidance; connect/timeout → host/port guidance).
  New `POST /email/test` route.
- New `docs/email-setup.md` (2SV → App Password → SMTP fields → Send test → troubleshooting),
  linked from the tightened Settings note. `test_email.py` covers the helper, error mapping, and the
  route (no network). Suite 63/63.

### 2026-07-13 — Automatic mileage capture (car Bluetooth → Dropbox → watcher) + saved routes
- **Automatic trips:** the phone (Android MacroDroid/Tasker; iPhone appendix included) fires on car-
  Bluetooth connect/disconnect, drops a one-line event file into a Dropbox folder, and the folder
  watcher (new third watch type + `trips_watch_folder` setting with Browse) ingests it. `trips.py`
  pairs connect→disconnect (12h window; driveway blips auto-skipped; stale danglers orphaned),
  routes road distance via the public OSRM server (haversine ×1.3 fallback flagged "estimated"),
  reverse-geocodes endpoints via Nominatim, and queues **trip candidates on /mileage** — edit miles,
  add a purpose, Approve → a normal mileage row; Dismiss for personal drives. Live-validated:
  Nashville→Franklin event files routed to 21.5 mi with real place labels.
- **Saved routes:** one-click chips on /mileage log a known trip as today ("Shop → McMaster = 23.4
  mi"); "save as route" checkbox on the manual form. Records-only throughout — nothing posts.
- Phone setup guide: `docs/mileage-automation.md` (MacroDroid + Dropsync or direct Dropbox API;
  Tasker; iOS Shortcuts appendix; Bouncie OBD noted as the odometer-grade upgrade path).
- `watcher.run_once/start` take an optional `trip_fn` (back-compat; `test_watcher` unchanged). New
  tables `trip_events`/`trip_candidates`/`saved_routes`. `test_trips.py` covers parsing, pairing
  edges, fallback math, watcher ingest idempotency, and the approve/dismiss/route flows. Suite 62/62.

### 2026-07-13 — Per Diem Travel page (GSA rates + per-diem vs actual-meals comparison)
- New Expenses → **Per Diem Travel**: log a trip (destination, city/state or ZIP, dates) and
  ShopBooks pulls the official **GSA M&IE rate** for the destination and fiscal year
  (api.gsa.gov per-diem API; optional api.data.gov key in Settings, shared DEMO_KEY default;
  lookup validated live — Nashville FY2026 returns $86 M&IE / $217 lodging). No locality or
  lookup unavailable → standard CONUS fallback; a manual per-trip rate always wins and can be
  set/overridden later ("Set M&IE rate" / "↻ Refresh from GSA").
- Trip detail computes the per-diem total (first/last travel day at 75%, single-day trips
  flagged) and compares it against **meals actually posted during the stay** (expense categories
  matched by name: meal/food/dining/restaurant/refreshment), with other in-range expenses and
  receipts listed as context/evidence — verdict card shows which deduction is larger and by how much.
- Tax guardrails built in: the per-diem election covers M&IE ONLY for the self-employed — lodging
  must be actual receipts (GSA lodging shown as a reference cap only); 50%-deductible note; advisor
  caveat. Records-only like the mileage log — nothing posts to the ledger.
- New `travel_trips` table (auto-created), `perdiem.py` module, `routes_travel.py` (get_con/
  safe_redirect plumbing), `gsa_api_key` setting. `test_perdiem.py`: fiscal-year math, GSA parse,
  75% breakdown edges, actuals matching, full /travel flow with mocked GSA. Suite 61/61.

### 2026-07-13 — Double-launch guard: reuse a healthy server instead of killing it
- Second real-world exe failure: the app window opened onto ERR_CONNECTION_REFUSED. Cause:
  `free_port()` kills whatever holds :8765 — right for stale servers, wrong when it's a HEALTHY
  instance. Double-clicking the exe twice (easy: a windowed exe shows nothing for seconds), or
  launching with a leftover window open, made launch #2 kill launch #1's server and orphan its
  window. Diagnosed live: a clean single launch of the installed exe served 200 for 40s+.
- Fix: `desktop.already_serving()` gates `main()` — if something on :8765 already identifies as
  ShopBooks, the new launch just opens another app window onto it and exits when that window
  closes, leaving the first instance's server lifecycle alone. (Also means launching the exe
  while the run.bat dev server is up reuses it instead of killing it.) Covered in
  `test_desktop.py` with a throwaway local HTTP server. Suite 60/60.

### 2026-07-13 — Fix: bundled ShopBooks.exe crashed on launch (windowed stdio)
- The Windows installer's exe died immediately with "Unable to configure formatter 'default'":
  a PyInstaller `console=False` build starts with `sys.stdout`/`sys.stderr` = None, and uvicorn's
  log formatter calls `sys.stdout.isatty()` while `uvicorn.Config` configures logging. macOS app
  bundles keep real streams, which is why only the Windows build hit it (and CI only proves the exe
  BUILDS, not that it launches).
- Fix: `desktop._shim_stdio()` runs at import, pointing any None stream at devnull BEFORE importing
  uvicorn/app (logutil attaches a console handler at import too). Reproduced first under pythonw
  (same None streams) with the identical traceback, then verified fixed the same way. Regression
  test added to `test_desktop.py` (simulates None streams in a subprocess). Suite 60/60.

### 2026-07-12 — Windows installer (ShopBooks-Setup.exe), mirroring the Mac build
- Windows is the primary customer channel, so it needed the same one-click install the Mac has.
  `desktop.py` was already cross-platform (Edge/Chrome app-window, `%LOCALAPPDATA%` profile,
  `netstat`/`taskkill` free-port), so the work was packaging: made `shopbooks.spec`
  platform-conditional (onedir `ShopBooks.exe` on Windows, keeping the mac `.app` path and
  single-sourcing `datas`/`hiddenimports`), added `installer.iss` (Inno Setup → per-user, no-admin
  `dist/ShopBooks-Setup.exe` with Start-Menu + Desktop shortcuts and an uninstaller; the books in
  `%USERPROFILE%\ShopBooks` are never touched), and a `build-windows.yml` GitHub Actions workflow
  (`windows-latest`) that builds it — PyInstaller/Inno can't cross-compile from macOS. Run it from
  the Actions tab or push a `v*` tag (attaches the installer to the Release). **Unsigned for now**
  (SmartScreen "More info → Run anyway"), signing left as a documented hook for a future cert. Also
  added a proper square ShopBooks "$" app icon (`static/app-icon.png`), now used by **both**
  builds — `build/ShopBooks.ico` on Windows, and `build-mac.sh` regenerates `build/ShopBooks.icns`
  from it too (always regenerated, no stale-icon guard) — instead of squishing the Outlier
  Workshop wordmark.

### 2026-07-12 — Folder picker for the Receipts "import a whole folder" field
- The Receipts page's whole-folder import still made you type a path by hand. Gave it the same
  server-backed folder picker the Settings folder fields use (built for #78). Extracted the picker
  modal into a shared `templates/_folder_picker_modal.html` include (so Settings and Receipts share
  one copy — no drift) and dropped a "📁 Browse…" button next to the folder input targeting
  `receipt_import_folder`. Reuses the existing `GET /settings/browse-folder` endpoint and
  `static/folder-picker.js` unchanged. `test_folder_picker.py` extended to cover the receipts page.

### 2026-07-12 — Dashboard: "Waiting for review" replaces "Recent activity" (issue #81)
- The dashboard's bottom section listed the last 12 **posted** ledger entries — a backward-looking
  log that duplicated what each account's register already shows. Replaced it with **unposted
  transactions waiting in Review** (pending `staged` rows), so the dashboard surfaces what still
  needs the owner. Section header shows the pending count as a badge with a "Review all →" link;
  each row is clickable through to `/review`; empty state is a positive "✓ Nothing waiting".
- `routes_dashboard.py` swaps the `recent` posted-entries query for a `pending` query using the same
  ordering/source-join as the Review queue itself (so the two never disagree), capped at 12 with a
  separate total for the badge / "showing N of M". Amount signs mirror Review (positive = money out,
  green negatives = money in). New `test_dashboard_pending.py`.

### 2026-07-12 — Folder picker for Settings (statement/receipt watchers, backup folder)
- The three watch-folder/backup-folder fields on Settings were plain text inputs — type or paste a
  path blind. Browsers deliberately never expose a real filesystem path from `<input type="file">`
  (privacy sandboxing), so a native-feeling picker has to come from the server, which already has
  full local filesystem access (CLAUDE.md invariant #8: local-only, no auth).
- New `GET /settings/browse-folder?path=...` (`routes_settings.py`) lists a directory's subfolders
  only (never files), skips dotfolders, and never 500s — an unreadable/missing path falls back to
  the home directory. New `static/folder-picker.js` + a small reusable `.modal`/`.folder-field` CSS
  block drive a "Browse…" button next to each field: navigate by clicking folders or "⬆ Up", or
  type/paste a path directly and hit Go — the modal writes the chosen path back into that field only
  (each of the three fields gets its own button, same shared modal).
- New `test_folder_picker.py`. Verified end-to-end in a live browser session: opened from a blank
  field (defaults home), descended into a folder, Up returned correctly, "Use this folder" wrote the
  exact path into the right field only, Escape and overlay-click both close it.

### 2026-07-11 — Signed Mac build: ShopBooks.app (+ app-mode window, shipping the standalone-app design)
- **`desktop.py`** (new): the launcher from `docs/standalone-app.md`, implemented as specced — frees
  port 8765, serves uvicorn in-process on a daemon thread, opens a chromeless Chrome/Edge **app-mode
  window** with a dedicated browser profile (own dock entry, persistent theme/column widths), and on
  window close stops the server **gracefully** so `sync.export_on_close()` still runs. Browser-tab
  fallback when no Chromium browser exists. `run-mac.command` now execs it (venv bootstrap unchanged).
- **`build-mac.sh` + `shopbooks.spec`** (new): PyInstaller onedir bundle → **`dist/ShopBooks.app`**,
  arm64, bundling Python 3.13 + deps + `templates/`/`static/` (both `__file__`-relative roots —
  `webutil.BASE`, `db.REPO_DIR` — resolve unchanged). Signed with **ad-hoc identity by default**
  (own Macs; first launch elsewhere = right-click → Open); `IDENTITY="Developer ID …"` switches to a
  real signature (hardened runtime + timestamp) and `NOTARIZE=1` runs notarytool + staple — the
  upgrade path needs no script changes. Artifact zip via `ditto`. Build dirs gitignored.
- Books stay in `~/Library/Application Support/ShopBooks`, outside the bundle — app updates never
  touch them. New `test_desktop.py` (import side-effect-free, helper behavior; found-by-harness: the
  first version polled the real :8765 and caught the owner's live server).

### 2026-07-10 — Route plumbing, final batch: closes #73
- Migrated the last 5 modules to `get_con`/`safe_redirect`: `routes_time.py` (mileage/time/jobs, 10
  connects), `routes_taxes.py` (7), `routes_reports.py` (7), `routes_recurring.py` (7),
  `routes_review.py` (5, the statement-import + Review-queue engine — the most intricate remaining
  file, with three internal helper functions that already took `con` as a parameter and needed no
  changes beyond their `back()` closures switching to `safe_redirect`).
- Caught during migration (not shipped): dropping the `import db` line from `routes_review.py` broke
  `db.DOCS` at runtime in `do_import` — a `NameError` invisible to `import app` or compilation, only
  triggered on an actual statement upload. Found and fixed via `pyflakes`, which was then run across
  all 16 migrated `routes_*` modules + `webutil.py` and found nothing else (one pre-existing, unrelated
  unused-variable warning in `routes_settings.py`, not a regression).
- **All 16 of 16 route modules migrated — #73 is done.** ~145 hand-rolled `db.connect()/try/finally`
  blocks and their copy-pasted redirect-quoting logic are now `get_con` + `safe_redirect` in
  `webutil.py`. Full suite 57/57; live GET smoke test across mileage, time, jobs (+ detail), taxes
  (+ package.zip), reports (+ pnl.csv), insights, forecast, chat, recurring, review, import — 14/14 200.

### 2026-07-10 — Route plumbing, part 6: routes_receipts, routes_settings (#73)
- Migrated `routes_receipts.py` (16 connects) and `routes_settings.py` (15 connects) to
  `get_con`/`safe_redirect`.
- Free cleanup: `backup_restore` opened and closed a DB connection it never used (`backup.looks_fresh`
  and `backup.restore` both work at the file level, not via the passed connection) — dead code,
  removed rather than migrated.
- 11 of 16 modules migrated (~98 of ~145 connects). Full suite 57/57; live smoke test on receipts,
  receipts/missing, accounts, rules, settings, and the duplicate-account error path (confirms
  `safe_redirect` quoting matches the old `quote()` output byte-for-byte on a real IntegrityError).
  Test accounts created during the live smoke test were deleted afterward; sync confirmed no drift
  reached the cloud copy (add+delete round-tripped to the same content hash).

### 2026-07-10 — Route plumbing, part 5: routes_invoices (#73)
- Migrated `routes_invoices.py` (23 connects, the largest module) to `get_con`/`safe_redirect`.
  Module-level helpers (`_active_items`, `_parse_line_items`, `_insert_line_items`, etc.) keep their
  exact signatures since `routes_estimates.py` imports three of them — verified the cross-module
  import + estimate pages still work after the migration.
- Free fix: `invoice_email` built its error redirect with a raw, entirely unquoted f-string
  (`f"/invoices/{invoice_id}?err=Email failed: {e}"` — a literal space and no `quote()` at all), so
  any SMTP error message would produce a malformed URL. Now goes through `safe_redirect`.
- 9 of 16 modules migrated (~67 of ~145 connects). Full suite 57/57; live smoke test on invoices,
  invoice detail/edit/pdf/summary, invoice-new, and estimates (cross-module check).

### 2026-07-10 — Route plumbing, part 4: routes_customers, routes_estimates (#73)
- Migrated `routes_customers.py` (10 connects) and `routes_estimates.py` (10 connects) to `get_con`.
  Hardcoded pre-encoded redirect strings (`?err=Customer+not+found`) now go through
  `safe_redirect(path, err=...)`, which URL-quotes with `%20` instead of literal `+` — functionally
  identical, just a different (correct) encoding style.
- 8 of 16 modules migrated (~44 of ~145 connects). Full suite 57/57; live smoke test on customers,
  customer detail/report, estimates, estimate-new, and the not-found redirect path.

### 2026-07-10 — Route plumbing, part 3: routes_reconcile, routes_migrate (#73)
- Migrated `routes_reconcile.py` (6 connects) and `routes_migrate.py` (6 connects) to `get_con`.
  `routes_reconcile.py`'s per-account redirects (`f"/reconcile/{account_id}?msg=..."`) now go
  through `safe_redirect(back, msg=...)`. `routes_migrate.py` keeps its own `_migrate_redirect()`
  helper as-is (already quotes correctly and always emits both msg/err params) rather than swapping
  it for `safe_redirect` — a deliberate minimal-footprint choice, not an oversight.
- 6 of 16 modules migrated (~24 of ~145 connects). Full suite 57/57; live smoke test on /reconcile,
  /reconcile/{id} (incl. the account-selector added earlier), /migrate.

### 2026-07-10 — Route plumbing, part 2: routes_dashboard, routes_feeds, routes_items (#73)
- Migrated the three smallest modules to `get_con`/`safe_redirect` per the established pattern
  (dashboard + global search routes, bank feeds, products/services catalog).
- Free fixes along the way: `routes_items.py` built redirect URLs with raw f-string interpolation
  (`f"/items?err={str(e)}"`), so an error message containing `&` or `#` could corrupt the query
  string or silently drop `msg=`; now goes through `safe_redirect`'s `quote()`.
- 12 of ~145 connect blocks migrated across 4 of 16 modules so far. Full suite 57/57; live pages
  smoke-tested (dashboard incl. period-comparison + AI-brief branches, search, items, feeds/settings).

### 2026-07-09 — Route plumbing, part 1: get_con dependency + safe_redirect (#73)
- `webutil.get_con()` (FastAPI `Depends` generator: one connection per request, closed in finally)
  and `webutil.safe_redirect(back, fallback, msg=, err=)` (in-app-path guard + URL-quoted msg/err)
  replace the hand-rolled `db.connect()/try/finally` and copy-pasted redirect guards. Handlers keep
  calling `con.commit()` explicitly — that is intentional.
- `db.connect()` now passes `check_same_thread=False`: FastAPI runs a sync dependency and the route
  handler on different threads; connections stay short-lived and sequential, so this is safe.
- First module migrated: `routes_entries.py` (12 connect blocks, 9 redirect guards). Also fixes two
  latent bugs there for free: entry_delete redirected to `back` unguarded on success (open
  redirect), and entry_edit did not URL-quote its error message. Full suite 57/57; remaining 15
  routes_* modules migrate one commit each.

### 2026-07-09 — Logging baseline (#74)
- New `logutil.py`: one `shopbooks` logger -> rotating `<datadir>/logs/shopbooks.log` (1MB x 3) +
  console. The log dir follows `db.DATA` (so `SHOPBOOKS_DATA_DIR` isolates it — tests never write
  logs into real books; `test_logutil.py` proves it).
- Added `log.warning(..., e)` before the silent fallbacks so a broken AI/feed/sync/watcher path is
  observable instead of indistinguishable from success: all 11 `return None`/`[]` swallows in ai.py,
  the receipt-AI read in staging.py, post-match categorization in routes_receipts.py, the watcher
  tick + per-file errors, cloud sync import/export failures, the statement-import handler, and chat.
  Observability only — no fallback behavior changed; ai.py still returns None, never raises. Harmless
  swallows (temp-file cleanup, date-sort fallbacks, feeds re-raising to the user) were left as-is.
  (Optional Settings log viewer left as a later nice-to-have.)

### 2026-07-09 — Code-quality: dedupe line-item JS + fold dashboard CSS (#72)
- Extracted the triplicated invoice/estimate line-item editor into one `static/line-items.js` (loaded
  via base.html); the templates keep only the Jinja `window.standardItems` bootstrap. `addRow()` is
  column-aware (adds the delete cell only on the edit page). Behavior unchanged.
- Moved dashboard.html's inline `<style>` block into `style.css` (`/* dashboard widgets */`) and
  fixed the rogue undefined `var(--text)` -> `var(--ink)` everywhere it appeared (dashboard + inline
  styles on customers/customer_detail/items) so text themes correctly in dark mode.

### 2026-07-09 — Carve app.py into domain routers (restores "thin routes")
- Fix #2 from the code-quality review: app.py had grown to 5,296 lines / 153 routes, contradicting
  the documented "routes only (thin)" architecture. It is now a **79-line composition root** —
  app creation, launch sequence (db.init → sync → snapshot), router includes, watcher wiring, and
  compat re-exports for the names tests import from `app`.
- **16 domain routers** (`routes_*.py`; largest is invoices at 864 lines), plus two shared layers:
  `webutil.py` (templates env, `ctx()`, `categories()`) and `staging.py` (the ingest→match→post
  engine used by review, receipts, the folder watchers, and bank feeds). Import graph is acyclic:
  webutil ← staging ← routes_* ← app; only routes_estimates imports from a sibling
  (routes_invoices' line-item helpers).
- Purely mechanical: blocks moved verbatim by a decorator-aware splitter; `@app.` → `@router.`;
  per-file imports generated from an AST walk of what each file actually uses. **No route paths,
  handler bodies, or behavior changed.** 56/56 test files pass; all 25 pages render.
- CLAUDE.md "Where things live" + ARCHITECTURE module map updated: add new routes to the matching
  `routes_*` module; cross-domain posting/matching logic goes in `staging.py`, never in a router
  another router would import.

### 2026-07-09 — Test harness that can actually fail + CI
- From the code-quality review: all 48 script-style test files reported failures with a print-only
  `ok = lambda ...` and **exited 0 regardless** — a failing suite could look green to any runner.
- New `testutil.ok()` keeps the exact PASS/FAIL output but remembers failures and forces exit code 1
  (atexit). Every test file migrated mechanically (`from testutil import ok`); the 8 assert-style
  files already failed correctly and are unchanged.
- New `run_tests.py` runs each `test_*.py` in its own subprocess (preserving the set-env-before-
  import isolation), fails on nonzero exit or a `FAIL` line, and **refuses any test file that
  doesn't set `SHOPBOOKS_DATA_DIR`** — the data-safety rule is now enforced mechanically. Full
  suite: 56 files in ~25s.
- GitHub Actions (`.github/workflows/tests.yml`) runs the suite on every push/PR; no secrets needed
  (AI tests force the AI-off path, feed tests monkeypatch HTTP). Also corrected CLAUDE.md's Python
  claim (the mac venv is the system 3.9, not 3.14; CI runs 3.13 — the suite passes on both).

### 2026-07-07 — Global search: live type-ahead
- Added a debounced type-ahead dropdown to the nav search box. New `search.suggest(con,q,cap)` (a
  lighter, small-LIMIT flat list; text-only invoice match so it is cheap per keystroke) served by a
  new `GET /search.json` endpoint (FastAPI serializes the list). New `static/search.js`: fetches on
  input, renders grouped items each linking to its detail page, with arrow-key highlight, Enter to
  open (or submit for the full /search page), Esc/outside-click to close; stale responses dropped by
  a sequence counter; HTML-escaped. Dropdown styled in style.css. Progressive enhancement — the plain
  submit still works without JS.

### 2026-07-07 — Global search
- New search box in the nav (every page) -> GET /search?q= results page grouping matches by type,
  each row linking to its detail page. New module search.py (run(con,q)); route in app.py.
- Interprets the query: text is matched case-insensitively (LIKE) across payee/memo, customer
  name/email/phone, invoice number/memo, account name, receipt vendor, staged description, job,
  mileage; an amount-looking query ALSO matches abs(amount_cents) (signs vary across tables).
  Invoice amounts are computed (invoicing.invoice_total) since no total column exists.
- Transactions have no detail page, so results deep-link to /register/{acct}#entry-{id}; added an
  id="entry-{id}" anchor to register rows + a tr:target flash highlight. Nav input styled to match
  the dark nav. All queries parameterized (injection-safe). test_search.py covers 15 cases.

### 2026-07-07 — Reconcile: correct the auto-detected account
- Statement upload (/reconcile/upload) auto-detects the account and jumped straight into it with no
  way to fix a wrong guess. Added an Account selector at the top of the per-account reconcile page
  (reconcile_account.html, fed by a new all_accounts list from the route) that switches account and
  carries the statement date & balance over. Detection is a heuristic; this restores manual control.

### 2026-07-07 — Review/reconcile UX fixes
- **Reconcile**: each account in the list now links to its register (the name is a link to
  `/register/{id}`), so you can jump straight to the transactions.
- **Review**: changing a transaction's category and then posting/skipping a *different* row no
  longer reverts the change. The review handler now persists all `cat_{id}` picks on every submit
  (mirroring the existing memo persistence), so edits survive the reload. Split-mode rows are
  unaffected (their single-category select is disabled and not submitted). Covered by
  `test_review_category_persist.py`.


### 2026-07-06 — Sales tax on invoices
- Each product/service and each invoice/estimate line has a **taxable** checkbox; a single
  business-wide **Sales tax rate** (Settings) applies. Invoices/estimates add a **Sales Tax line**
  (Subtotal → Sales Tax (rate%) → Total) on screen and in the PDF; taxable lines are marked "(tax)".
- Totals are now **tax-inclusive** (`invoice_total = subtotal + tax`), so balances, AR aging, and
  payment reconciliation all account for the tax owed. Picking a catalog item auto-fills its taxable
  flag onto the line (per-row hidden field keeps form alignment).
- **Collected tax is booked as a liability, not income.** Recording a payment splits it
  `[(bank, +P), (income, −subtotal share), (Sales Tax Payable, −tax share)]` (proportional on partial
  payments); `invoice_payments_total`/`invoice_payment_entries` count the tax leg so tax-inclusive
  invoices still reconcile. A "Sales Tax Payable" account is seeded and ensured on every launch.
  Non-taxed invoices are unchanged (single income leg).
- Schema: `items.taxable`, `invoice_items.taxable`, `sales_tax_rate` setting (guarded migrations).
  New committed `test_sales_tax.py`; full invoicing/customer/items regression suite passes.
  Limitation: matching an already-booked deposit doesn't retroactively split tax (documented).


### 2026-07-06 — Harden item linkage when a catalog item is deactivated
- Edge case from the item_id wiring: if a product/service was **deactivated** after being used on an
  invoice, it dropped out of the (active-only) line dropdown, so editing the invoice re-posted an
  empty `item_id` and silently broke the linkage. `invoicing.get_invoice` now LEFT JOINs the catalog
  (adding `item_name`/`item_active` per line), and the edit form renders the line's current item as a
  selected **"(inactive)"** option in its own row — so an unrelated edit keeps the link, while the
  user can still switch to an active item. Covered in `test_review_fixes.py`.

### 2026-07-06 — Fix statement payments; wire invoice lines to the catalog (review follow-ups)
- **Customer statement report** now counts multi-payment invoices correctly. It was totaling
  payments from only `paid_entry_id`/`matched_entry_id`, so an invoice paid by several deposits
  linked via `invoice_entry_links` showed missing payment rows and an overstated balance. New
  `invoicing.invoice_payment_entries()` mirrors `invoice_payments_total`'s priority (single full
  payment → all linked payments → matched deposit); the report reuses it, so the statement now
  reconciles with the invoice's outstanding balance.
- **Invoice/estimate lines now persist `item_id`** — the previously-inert column added with the
  Products & Services catalog. Picking a catalog item records the linkage (on create, edit, and
  estimate→invoice conversion), enabling future "sales by product/service" reporting; manual lines
  store NULL. Line-item parsing/insertion consolidated into `_parse_line_items`/`_insert_line_items`,
  and the item dropdowns now survive validation-error re-renders and the estimate edit page.
- New committed `test_review_fixes.py` (multi-payment statement, item_id on create/edit/convert,
  NULL for manual lines). Invoice/estimate/customer suites still pass.


### 2026-07-04 — Edit a posted entry into a split (register ⇔ Split editor)
- Follow-up to split transactions: you can now split an **already-posted** entry, or re-allocate an
  existing split, straight from a register — no delete-and-re-enter. Each row gets an inline
  **⇔ Split** editor, prefilled with the entry's current categories and its money-in/out direction
  (derived from the register-account leg's sign), with add/remove rows and a live total.
- `ledger.rewrite_entry_splits(con, entry_id, anchor_account_id, [(cat_id, magnitude_cents), …],
  direction)` replaces the category legs and recomputes the anchor leg (`-Σ categories`) so the entry
  stays balanced; header + receipt/invoice links are untouched, locked periods are still guarded.
  Route: `POST /entry/{id}/splits` (same field shape as `/entry/new`).
- The register lists a split's categories (comma-joined) and the old single-field inline editor now
  says "use ⇔ Split to edit" for multi-category entries instead of showing a misleading single
  dropdown. `test_splits.py` extended (simple→split, re-allocate, empty-submission no-op, zero-sum).

### 2026-07-04 — Split transactions: multiple categories on one entry
- A fundamental bookkeeping capability: allocate one transaction across several categories
  (e.g. a $100 store run as $60 Supplies + $40 Office). The ledger already supported arbitrary
  balanced splits (`post_entry`); this adds the two write surfaces where you actually split.
- **Review:** each pending row gets an inline "⇔ Split across categories" drawer — add category
  rows with amounts, with a live "of $X — balanced ✓ / $Y left" indicator against the row total.
  `_post_staged` grew a `splits=[(category_id, magnitude_cents), …]` mode; magnitudes must sum to
  the row's absolute amount or **nothing posts** (a mis-typed split can't book a wrong entry).
  A split leaves `staged.category_id` NULL and skips the single-category-only conveniences
  (transfer post-once, invoice auto-mark, remember-as-rule).
- **Manual entry (`/entry/new`) rebuilt** around one money account + a money-in/out direction +
  N category rows (with "+ Add split" and a live total), replacing the raw to/from debit-credit
  form with something friendlier that also splits. Rejects a category that equals the source.
- Posting formula generalized and documented (CLAUDE.md invariant #3, ARCHITECTURE.md §Posting
  formula): `[(Cᵢ, sign·mᵢ), …, (source, −a)]`. Registers already render multi-leg entries as
  "(split)". New committed `test_splits.py` (manual out/in, self-ref reject, staged balanced +
  unbalanced, zero-sum invariant); transfers/bulk/receipt/invoice-staged tests still pass.

### 2026-07-04 — UI glowup: design-system refresh, dark mode, command-center dashboard
- Presentation-layer only — no ledger/route logic changed beyond passing one extra value to the
  dashboard. Zero risk to the invariants.
- **`static/style.css` fully tokenized.** Every color is now a CSS variable; light values in
  `:root`, a `:root[data-theme="dark"]` block overrides them. All existing class names kept
  (restyled, never renamed), so every page inherits the refresh: sticky gradient nav, softer
  shadows/radii, card hover-lift, refined tables, buttons, inputs, callouts.
- **Dark mode.** `base.html` sets `data-theme` on `<html>` before first paint (no flash):
  explicit choice from `localStorage['sb-theme']` wins, else follows the OS `prefers-color-scheme`.
  A round toggle button (☾/☀) in the nav flips and persists it. Pure client-side; no server state.
- **Dashboard rebuilt (`dashboard.html`) into a command center:** hero "Today at a glance" panel
  (gradient accent bar, attention list, cash/card/receivables/next-tax stat row), quick-action
  pill bar, KPI tile grid (accented net-profit tile), and an inline-SVG sparkline of monthly net
  profit fed by `insights.monthly_trend` (the one new value passed from the `/` route), plus the
  existing recent-activity table.

### 2026-07-03 — Duplicate handling: tighter prevention + a detection safety net
- Two-pronged, prompted by "what's the best way to handle duplicates getting posted." All prior
  dedupe (feed txn ids, receipt hashes, statement filename/content, feeds' cross-source check) fires
  at the import boundary; nothing re-examined the posted ledger.
- **Prevention tweak**: `feeds._already_on_books` matched a same-amount statement twin only on an
  EXACT date — but a feed and a statement routinely post the same charge a day or two apart, so the
  feed twin slipped past and double-posted. Widened to a ±`CROSS_SOURCE_DAYS` (3) window, matching
  the philosophy of the statement re-import guard's existing ±2-day check.
- **Detection safety net**: new `duplicates.py` + `/duplicates` page (nav: Bookkeeping → Find
  Duplicates). Finds posted entries on the same bank/card account for the same signed amount within a
  4-day window, chained into groups. Anchored on the money-movement leg so each pair is reported once
  (not twice via the shared category). Never auto-deletes — a genuine repeat is indistinguishable by
  amount, so it surfaces candidates and the owner checks which to remove (reusing `ledger.delete_entry`,
  so deleted imports revert to Review; locked-period entries are skipped, not aborted). This is the
  honest catch-all: cross-source duplicates are inherently heuristic (a statement and a feed can't
  share a txn id), so prevention keeps the list short and detection covers the rest.
- `test_duplicates.py` (24 assertions): window boundaries, chained runs, same-amount-different-account
  separation, once-not-twice reporting, the widened feeds guard, and the delete/skip HTTP surface.
  Full suite green (incl. test_feeds.py — the guard change didn't regress).

### 2026-07-03 — Folder watchers for statements and receipts
- New `watcher.py`: a lightweight polling thread (not a system daemon — only runs while ShopBooks
  itself is running, started at app boot / stopped at shutdown, same lifetime as the existing
  backup/sync-on-boot behavior) that checks two configurable folders about once a minute for new
  files. Deliberately generic — the scan/dedupe engine (`scan_folder`/`run_once`, backed by a new
  `watched_files` path+mtime+size table) knows nothing about statements or receipts; app.py supplies
  `_watch_statement`/`_watch_receipt` callbacks that reuse the exact existing pipelines (single-step
  statement import incl. `importer.is_duplicate_statement`; `_ingest_receipt`'s content-hash dedupe).
- Settings → "Folder watchers": two folder-path fields (blank = off) + a "Scan folders now" button
  and last-scan status. Dropped files land PENDING in Review or as a receipt document — identical to
  a manual upload; nothing ever posts automatically.
- `db.connect()` gains `PRAGMA busy_timeout=5000` — this is the app's first real background thread
  writing to SQLite concurrently with request handlers, so a brief lock now waits instead of erroring.
- `test_watcher.py` (29 assertions): the generic engine (new/changed/unchanged file handling, missing
  folder, a process_fn exception caught not raised), the real statement pipeline (correct account
  auto-detection, staging, duplicate detection across a renamed file), the real receipt pipeline
  (content-hash duplicate detection), the HTTP surface, and thread start/stop lifecycle. Full suite
  green — confirmed `@app.on_event("startup")` never fires under the test suite's
  `TestClient(app.app)` pattern (no `with`), so no test spins up a real background thread.

### 2026-07-03 — Bulk select on Register and Review
- Prompted by cleaning up a batch of wrong-signed feed transactions by hand (delete one at a time).
  Register: checkbox per row, "Delete selected" and "Apply category to selected" (2-split entries;
  split entries and locked-period entries are skipped, not aborted). Review: checkbox per row, "Apply
  to selected" (bulk category), "Post selected", "Skip selected".
- All bulk routes loop the existing single-entry primitives (`ledger.delete_entry`,
  `ledger.update_entry_fields`, `_post_staged`) per selected id — no new ledger logic, so correctness
  inherits from what's already tested. `register_bulk_category`/`register_bulk_delete` report a
  locked-period skip count rather than failing the whole selection.
- `test_bulk_actions.py` (23 assertions): only selected ids are touched, split/locked entries are
  skipped correctly, an empty category selection redirects with a friendly error (not a 422), and the
  HTTP surface (checkboxes, redirects) is wired up on both pages. Full suite green.

### 2026-07-02 — Bank feeds via SimpleFIN (#43, un-parked)
- #43 was parked assuming Plaid; re-researched: SimpleFIN Bridge ($15/yr, read-only by design, plain
  HTTPS+JSON) covers 4 of the 5 accounts — Eastern Bank (incl. a Treasury & Business connector), Chase,
  Amex, Capital One. QuickBooks Checking isn't connectable via any aggregator (stays on statement
  import). Teller checked too: only 2/5. Bank credentials never touch ShopBooks — the owner connects
  banks on the bridge's site; the app claims a one-time setup token into a durable read-only access URL
  (stored like the other secrets; revocable from the bridge dashboard).
- New `feeds.py` + `feed_accounts`/`feed_txns` tables: one GET pulls every account (bridge allows ~24
  req/day; manual Fetch button, no daemon); posted-only (pending mutate and would fight dedupe); signs
  converted from the bank's balance perspective to staged money-out-positive; one `feed:` batch per
  mapped account, staged via `importer.stage_transactions` — so rules/history/AI categorization,
  transfer pairing, and the human-confirmed Review step all apply exactly as for a statement import.
- Two dedupe layers: exact SimpleFIN txn ids (7-day window overlap absorbed) + a same-account
  date+amount guard against statement-import overlap.
- Settings → "Bank feeds (SimpleFIN)": connect (paste setup token), map feed accounts to bank/card
  accounts, enable/disable, Fetch now, Disconnect. Import page gets a Fetch button when connected.
- `test_feeds.py` (31 assertions, zero network — HTTP layer mocked): claim, signs both directions,
  pending exclusion, both dedupe layers, unmapped/disabled handling, PENDING-only guarantee, routes.
  First real fetch (owner's token) should eyeball signs in Review before posting.

### 2026-07-02 — Estimated-tax reminders & payment tracking (#40)
- New `tax_payments` table: record each 1040-ES payment (keyed to the TAX year+quarter — Q4 paid in
  January still counts toward the prior year). Record/remove on the Taxes page ("Estimated Payments
  Made" section); the quarterly table gains Paid + Remaining columns (overpayment shown explicitly).
- `insights.estimated_taxes` now returns per-quarter `paid`/`remaining` (+ year totals) — additive, all
  existing fields unchanged.
- The dashboard briefing reminder now uses what's **still due** (fully-paid quarters never nag, partial
  payments shrink the number), escalates to a warning within 7 days of the due date, and catches last
  year's Q4 (due Jan 15) in early January.
- `test_tax_payments.py` (20 assertions): due/paid/remaining math incl. partial + overpay, tax-year
  attribution, the briefing skip/escalate/January-edge behavior, and the record/reject/delete routes.
  Closes the last substantive milestone issue (#40).

### 2026-07-02 — Auto-detect recurring bills from posted history
- New `recurring.detect_candidates(con)`: scans the last 12 months of posted 2-split entries (one
  bank/card leg + one real category leg — transfers and Uncategorized are excluded by construction),
  groups by normalized vendor (`importer.payee_key`) + category + account, and suggests a template for
  any vendor seen >= 3 times on a regular weekly/monthly/yearly cadence (median-gap bands + a 70%
  regularity check). Amount = median occurrence; a pattern whose last occurrence is stale (> ~1.8
  periods ago) or that already has a template (active OR paused) is never suggested.
- Recurring page: a "Suggested from your history" table — each row one-click Creates via the existing
  `POST /recurring` route (hidden prefilled form; nothing is created automatically). Once created, the
  suggestion disappears.
- Deterministic, no AI. `test_recurring_detect.py` covers grouping/median/frequency inference, all seven
  exclusion cases (one-off, too-few, irregular, dead, uncategorized, transfer, templated), and the
  render + one-click-create flow. Full suite green.

### 2026-07-02 — Model agility: Assistant follows `ai_model`; swap-safe token budgets
- `chat.py` hardcoded `claude-opus-4-8`, so changing the model in Settings changed every AI feature
  *except* the chatbot. It now uses `ai._claude_model(con)` like everything else. The only remaining
  model strings are the `ai_model` default (db.py) and `_claude_model`'s fallback.
- Raised `ai.analyze` max_tokens 900→4000 and receipt reads 1000→2000. `max_tokens` is a cap, not a
  spend — no billing change on Opus 4.8 — but it removes the truncation trap for models whose thinking
  tokens count against the budget (e.g. Fable 5, where thinking is always on).
- Decision recorded after reviewing Fable 5 vs Opus 4.8 vs Sonnet 5 for the app: **keep `ai_model` =
  claude-opus-4-8**. The deterministic-ledger design keeps model tasks easy (extract / pick from a list /
  narrate), so Fable 5's 2× price + always-on thinking + refusal handling buys nothing here; the cost
  lever points the other way — optionally set `categorize_model` to a cheaper model in Settings.

### 2026-06-29 — Fix: receipt-files mirror no longer breaks/alarms the books sync
- Symptom: a red "Cloud sync hit a problem reading the cloud copy" banner + `Sync: error ([Errno 1]
  Operation not permitted: '…/_sync_docs')`, even though the books DB was actually in sync. Cause: macOS's
  Dropbox/CloudStorage File Provider denying the process *directory* access to the `_sync_docs` receipt
  mirror.
- `sync._mirror_files` only caught per-file copy errors, so a directory-level `OSError` (mkdir/iterdir/exists)
  escaped and was caught by `_import` / `export_on_close` as a generic sync error — and since the docs step
  runs before the DB step, it could block new book changes from syncing.
- Fix: wrap the directory-level ops in `_mirror_files` in `try/except OSError: return 0`, so the receipt
  mirror is strictly best-effort — a denied/undownloaded `_sync_docs` can never abort or alarm the books
  sync; it just retries next round. `test_sync.py` gains a case (#16) simulating the EPERM and asserting the
  DB still exports/imports. (Getting receipts to actually mirror again still needs Full Disk Access for the
  app — a one-time macOS Privacy setting, not a code change.)

### 2026-06-29 — Cash-flow forecast (#38)
- `insights.cash_forecast` finished and wired up: projects month-end bank cash over a 30/60/90/180-day
  horizon from starting cash + expected invoice collections (by due month) + recurring income, minus
  recurring bills and a trailing-average "variable burn". Recurring expenses are carved out of the average
  and placed explicitly (so a yearly bill spikes in its month) without double-counting. Flags the low point
  and any dip below $0.
- New `/forecast` page (Reports → Cash-Flow Forecast): summary cards, a month-by-month table, a horizon
  selector, and an optional "✨ Explain" AI readout (AI-optional). The dashboard briefing now warns when cash
  is projected to go negative.
- `test_forecast.py` covers the figures, the per-month projection, recurring carve-out (no double-count),
  the low point, and the goes-negative path. Full suite (39 files) green. Completes the proactive set.

### 2026-06-29 — Customer credits: visibility, apply-from-memo, overpayment→memo
- Surface unused credit (#1): per-customer "Credit" column on the Invoices page, and an "$X in unused
  customer credit to apply" item in the dashboard briefing. New `invoicing.available_credits_for_customer`
  / `customer_available_credit` / `available_credit_total` (single home for credit-source math; app.py now
  delegates to it).
- Apply from the credit-memo side (#2): a credit memo with credit left shows the customer's open invoices and
  an Apply form (`/credit-memos/{id}/apply`). Shared `_apply_credit_core` powers both directions (caps at the
  source's available credit AND the target's balance).
- Overpayment → credit memo (#4): an overpaid invoice offers one-click "Create credit memo from overpayment";
  the excess is moved into a new CM (recorded as an application onto it) so it's never double-counted.
- `test_customer_credits.py` extended: apply-from-memo, overpayment→memo (with a no-double-count assertion),
  and the briefing surfacing. Full suite green.

### 2026-06-29 — Customer credits / credit memos
- New `credit_memo` document kind (invoices row, CM- numbering) + `credit_applications` table: issue a
  credit memo, or use an overpaid invoice's excess, and apply it against open invoices.
- `invoicing.invoice_outstanding_balance` is now the single source of truth (total − payments − applied
  credits; negative = available credit). Statuses re-evaluate via `_update_document_status`; AR aging and
  the customer outstanding totals net credits; the invoice PDF labels credit memos. Apply / Remove credit
  from the invoice view; void/delete cleans up applications and re-evaluates the linked docs.
- Hardening: applying a credit is now capped at the **target invoice's remaining balance** (not just the
  source's available credit), so over-applying can't silently waste credit — the unused remainder stays
  available. `test_customer_credits.py` covers the full apply→revert→overpay→reapply flow plus the cap.

### 2026-06-28 — Partially Paid Invoices & Customer Payment Tracking
- Added a `customer_id` foreign key column to the `entries` table schema, with a startup migration to automatically backfill customer IDs for all existing matched payments.
- Support `partially_paid` invoice status when matched payment totals are less than the invoice total.
- Updated A/R aging queries, invoices listing, and dashboard briefing to bucket and display open invoices using only their *outstanding balance* rather than the full total.
- Updated PDF invoice rendering to print a summary showing *Total*, *Payments/Credits*, and *Remaining Balance Due* when payment history is present.
- Updated `/invoices/{invoice_id}/pay` (Record Payment) to post a deposit for the remaining outstanding balance, and updated `/invoices/{invoice_id}/unpay` to revert the status back to `partially_paid` if other matches remain.
- Enabled customer association directly on checking/income entries: added a Customer column in the register view (`register.html`) with inline dropdowns to link/unlink transactions to customers, a POST `/entry/{id}/customer` route, and updated `/entry/edit/{id}` to store the customer.
- Added a new test suite `test_invoice_partially_paid.py` to cover partially paid state transitions, posted balance payments, and customer link sync. All 40 test suites pass.

### 2026-06-28 — Multi-payment matching for Invoices
- New `invoice_entry_links` table + database migration automatically backfilling from legacy `matched_entry_id` column.
- Added a collapsible, roll-out pairing drawer under the invoice details view (`invoice_view.html`) showing a checkbox list of available deposits on books to match with the invoice.
- Updated `app.post("/invoices/{invoice_id}/save-matches")` to record multi-deposit associations, update the invoice's paid status, and set the paid date to the latest matched deposit's date.
- Updated `ledger.delete_entry` to re-evaluate remaining matches and dynamically update the invoice status and paid date when any linked deposit is deleted.
- Created `test_invoice_drawer.py` covering multi-payment matching, date recomputation on deletion, and unmatching flows. All 39 test suites pass.

### 2026-06-28 — Recurring transactions / predicted monthly bills (#39)
- New `recurring` table + `recurring.py`: templates for predictable bills/income (rent, subscriptions,
  loan payments). Frequencies weekly/monthly/yearly; month/year steps clamp to the month's last day.
- Human-confirmed posting (nothing auto-posts): a due occurrence is one click — `post_occurrence` posts the
  ledger entry (via `ledger.post_entry`, so the period lock is respected) and advances `next_date`; skip
  advances without posting; pause/resume + delete. New Recurring nav page (due-now section + "Post all due"
  + add form). The dashboard briefing (#37) now flags "N recurring bill(s) ready to post".
- `recurring.upcoming(con, start, end)` projects future occurrences (signed) — the substrate the cash-flow
  forecast (#38) will build on.
- `test_recurring.py` covers the date math (incl. Jan-31→Feb-28 and leap-day clamping), due detection,
  post/skip, income vs expense direction, the period-lock guard, and the projection. Full suite (38) green.

### 2026-06-28 — Proactive dashboard briefing: "what needs me today" (#37)
- New `insights.briefing(con, today)`: one deterministic snapshot tying together cash on hand + card debt,
  receivables (total/overdue, reusing ar_aging), the next estimated-tax due date/amount, and a prioritized
  `attention` list (waiting in Review, overdue invoices, accounts out of balance, unmatched/missing receipts,
  Uncategorized Expense) — each item with a link to act on it. `all_clear` when nothing's outstanding.
- Dashboard leads with a "Today" panel (attention list + cash/AR/next-tax summary). Optional on-demand AI
  one-liner via "✨ Brief me" (`/?brief=1` → `ai.analyze`); AI-optional, no cost unless clicked.
- Reusable by the Assistant too (same deterministic figures). `test_briefing.py` covers the empty all-clear
  case and a seeded mix (cash math, AR, and each attention item with its link). Full suite (37 files) green.

### 2026-06-28 — AR aging + overdue-invoice reminders (#36)
- New `invoicing.ar_aging(con, today)`: every open (sent, unpaid) invoice bucketed by age
  (current / 1-30 / 31-60 / 61-90 / 90+) with totals — deterministic, from the line items.
- Invoices page: an "Outstanding (accounts receivable)" section (bucket cards + open-invoice list with
  days-overdue and last-reminded). Dashboard: an "Owed to you" card (total + overdue) linking to Invoices.
- Overdue reminders (opt-in, reuse SMTP): per-invoice "Send reminder" on the invoice + AR list, and a bulk
  "Send reminders to overdue" that skips any reminded within 7 days. New `invoices.last_reminder_date`
  (guarded migration) stamps each send; editable `reminder_subject`/`reminder_body` settings (sensible
  defaults). Reminders only ever send when you click — there's no background daemon in a local app.
- `test_ar_aging.py` covers the buckets/totals/exclusions and the reminder dispatch/skip/stamp logic
  (SMTP + PDF stubbed). Full suite (36 files) green. Closes the get-paid loop (estimates #35 → invoices → AR).

### 2026-06-28 — Estimates / quotes that convert to invoices (#35)
- Estimates are `invoices` rows with a new `kind` column ('invoice' | 'estimate') + `converted_invoice_id`
  (guarded migration; own EST- number sequence via `next_estimate_number`). They never post to the ledger,
  never match deposits, and never appear in the invoice list or AR — every existing invoice/match/categorize
  query is scoped to `kind='invoice'`.
- New Estimates section (nav): list, create (line items, "valid until" date), view, kind-aware PDF
  (ESTIMATE header, "Valid Until", "Estimated Total"), and email. Status flow draft → sent → accepted/declined.
- One-click **Convert to invoice**: copies the line items into a new INV- invoice (date today, due +30d), marks
  the estimate accepted and links it; re-converting just returns the existing invoice (no duplicate). Reuses the
  existing invoice PDF/email machinery.
- `test_estimates.py` (HTTP) covers create, ledger-isolation, invoice-list exclusion, the /invoices→/estimates
  guard, conversion (items copied + linked), and idempotent re-convert. First step of the get-paid loop (#36 next).

### 2026-06-28 — Reconciliation Phase 2: per-transaction clearing (trustworthy books, part 2; #34)
- New `splits.reconciled_id` (guarded migration) marks an account-leg as cleared in a reconciliation.
- `reconcile.cleared_balance` / `unreconciled_transactions` / `finish`: tick the transactions that appear
  on a statement; when the cleared balance equals the statement's ending balance, finish stamps those
  splits reconciled and records the checkpoint (difference computed from the CLEARED balance, not the whole
  book). Cleared items carry forward as the next statement's beginning balance and drop off the checklist.
- `/reconcile/finish` route + a live checklist on the account page (running cleared total / difference,
  select-all, Finish enabled only at $0 difference). The quick balance-check + square-up adjust stay as a
  secondary tool. Works for assets and liabilities (display-signed). `test_reconcile_clearing.py` covers
  carry-forward, the out-of-balance case, and a credit card. Completes "trustworthy books".

### 2026-06-27 — Year-end close / period lock (trustworthy books, part 1)
- New synced setting `books_locked_through`: once set, transactions dated on or before it are frozen —
  they can't be added, edited, or deleted. Lock a filed year so its numbers can't change by accident.
- Enforced at the ledger chokepoint so it's airtight from every screen: `ledger.assert_unlocked()` guards
  `post_entry`, `delete_entry`, and `update_entry_fields`. New `ledger.LockedPeriodError(ValueError)` so the
  existing `except ValueError` route handlers surface a friendly message; added that handling to `/entry/delete`.
- UI on the Taxes page: a "Year-end close" box (close through a date — defaults to Dec 31 of the shown year —
  with a reopen button) plus a "Books closed through X" banner. The lock is a normal setting, so it syncs to
  the other machine (a closed year stays closed everywhere).
- `test_period_lock.py` covers posting/deleting/editing into a locked period, moving an entry into it, the
  on-or-before boundary, and reopening. Part 2 (reconciliation Phase 2 — per-transaction clearing) is next.

### 2026-06-26 — Schedule C Mapping & Estimated Quarterly Payments
- **Schedule C Tax Mapping**: Added support for mapping income and expense accounts to standard IRS Schedule C lines directly via dropdown selectors on the Chart of Accounts page (`/accounts`).
- **Estimated Quarterly Taxes**: Integrated quarterly estimated tax calculations, computing Self-Employment Tax (15.3% SE tax on 92.35% of net profit) and Estimated Income Tax based on a configurable rate.
- **Tax Dashboard**: Redesigned the Taxes page (`/taxes`) to present a draft Schedule C P&L report, a warning checklist for unmapped active categories, an Estimated Quarterly Payments table showing Q1-Q4 due dates and payments, and an inline form to adjust the estimated income tax rate.
- **Tax Package ZIP**: Added `{year}_schedule_c.csv` mapping report inside the generated tax package ZIP.
- **Tests**: Created `test_taxes_schedule_c.py` verifying schema migrations, mapping updates, tax reports, quarterly payments, and ZIP packaging.

### 2026-06-24 — Assistant: the Opus chatbot (issue #7)
- New `chat.py` + `/chat` page ("Assistant" in the nav): a conversational helper whose three jobs are
  (1) how to use ShopBooks, (2) general tax strategy for a sole proprietor, (3) business analysis on the
  owner's REAL numbers.
- Architecture: an agentic tool-use loop on `claude-opus-4-8` (adaptive thinking). Claude is given the
  `insights.py` / `timetracking` read-only functions as tools (business_snapshot, profit_and_loss,
  compare_periods, monthly_trend, expense_changes, cash_position, bookkeeping_health, missing_receipts,
  jobs_overview). It must fetch every figure from a tool — it never invents or computes numbers. The tool
  layer converts integer cents → dollars so the model only reports, keeping the ledger deterministic.
- AI-optional: with no Anthropic key, the page explains how to turn it on; `chat.ask()` returns a friendly
  off-message and makes no network call. Transcript is in-memory (single local user; resets on restart),
  with suggested-prompt chips and a Clear button. Tools re-run each turn, so follow-ups stay grounded.
- `test_chat.py` covers the tool dispatch, cents→dollars conversion, schema well-formedness, graceful
  tool errors, and the AI-off path — all without a network call. Closes the wishlist capstone (#7).

### 2026-06-24 — Inline transaction editing in register views
- Added in-place editing of date, payee, memo, category, and job directly on the register tables.
- Eliminates the need to delete and recreate entries for simple corrections, preserving database-level relationships (invoices, receipts, staged logs).
- Added test_edit.py to the test suite to verify editing flows and splits-sum-to-zero invariants.

### 2026-06-23 — Missing-receipts report (issue #5)
- New `insights.missing_receipts(con, start, end, min_cents)`: posted EXPENSE transactions with no
  receipt attached (excludes income/transfers and anything already matched), >= a $ threshold.
- New `/receipts/missing` page (period + min-amount filters; count + total undocumented), linked from
  the Receipts page; and a tax pre-flight checklist row "Expenses with a receipt" linking to it for
  the year. Surfaces what lacks documentation at tax time. test_insights.py covers it.
- (Fuzzy-vendor match improvement from #5 left as a later refinement; amount+date matching stays.)

### 2026-06-23 — Cache-bust static assets + Review column reorder & visible resize grip
- Static assets (CSS/JS) now carry a `?v=<newest-static-mtime>` token via a Jinja global `static_v()`,
  so a browser always re-fetches them after a change. (A stale manual `?v=1.5` on style.css had been
  pinning the old stylesheet, so resize/width changes never showed no matter how you refreshed.)
- Resize divider is now an always-visible grip (grey, green on hover) with a wider grab zone; header
  no longer clips it (overflow moved to td only).
- Review columns reordered to: Date, Remember, Post/Skip, Description, Source, Amount, Category, Memo
  (quick-action columns up front). Category default widened to 320px. Mobile card view remapped to match.

### 2026-06-23 — Right-sized + draggable table columns (Review)
- The Review table is now `table-layout: fixed; width: auto` with a `<colgroup>` of sensible default
  widths, so columns are only as wide as needed (Description no longer hogs; Category is wide enough
  to show full names instead of truncating). Wrapped in `.table-responsive` so a wide table scrolls.
- New `static/resize.js`: any `table.resizable` gets a drag handle on each column edge. Dragging a
  divider changes only that column; columns to its right keep their size and shift (the table grows/
  shrinks, never steals from the neighbor). Widths persist per page in localStorage. Handle swallows
  the click so it does not also trigger sort. Divider is an always-visible grip (green on hover).

### 2026-06-23 — Repoint receipt paths on docs-backfill (receipts unreachable after data move)
- Receipts showed the "file not on this computer" placeholder on the PC even though the files had
  synced down, because `_repoint_doc_paths` only ran during a full DB import (fast_forward) - not
  during the additive docs backfill on every pull/boot. After the data dir moved (%LOCALAPPDATA%
  -> %USERPROFILE%), rows still pointed at the old/foreign docs folder, so /doc 404 -> placeholder.
- Fix: `sync._import` now also runs `_repoint_doc_paths` after `_pull_docs` when no DB import ran
  (idempotent; basename preserved). Updated the /doc placeholder text + stale comment (files DO sync).
  Verified live: after restart all receipts resolve on the PC.

### 2026-06-23 — Insights / analysis page (issue #6)
- New /insights page (Insights nav) surfacing the deterministic numbers: income/expense/net growth
  vs the prior period, monthly-net trend bars, biggest expense movers (insights.expense_changes),
  profit-by-job, cash position, and bookkeeping health. Period selector (this/last year, quarter, month).
- Optional on-demand AI readout: '✨ Explain these numbers' (ai.analyze) writes a plain-English summary
  from the exact figures. AI-optional — the numbers always render; button hidden when AI is off; returns
  None on failure. No API cost unless clicked.
- Builds entirely on insights.py + timetracking job costing. test_insights.py covers expense_changes.

### 2026-06-23 — Fix cross-OS receipt paths after sync (PC receipts unreachable on Mac)
- After pulling the PC's books, every receipt opened as the "file not on this computer" placeholder
  even though the files had synced. Cause: `sync._repoint_doc_paths` used `pathlib.Path(p).name`,
  which doesn't split Windows `\` on macOS/Linux — so a Windows path `C:\...\docs\rcpt.txt` kept its
  whole self as the "basename" and got stored as `<docs>/C:\...\rcpt.txt` (nonexistent).
- Fix: new `sync._doc_basename` strips both `/` and `\` separators; repoint now resolves correctly
  regardless of which OS wrote the path (and recovers already-mangled paths on the next import).
  Repaired the live Mac books (75/75 receipts now resolve). `test_sync.py` covers Windows + POSIX.

### 2026-06-23 — Optional memo on imported transactions
- `staged` gains a `memo` column (guarded ALTER in `_column_migrations`). The Review table now has
  an optional Memo input per row; typed memos persist on any form submit and are carried onto the
  posted entry (`_post_staged` -> `ledger.post_entry(..., memo=...)`). Manual entry already had memo.
- Lets you annotate bare/ambiguous imported rows before posting. Verified: column migrates without
  data loss, memo flows staged -> entry, migration idempotent.


### 2026-06-23 — Make AI categorization transfer-aware (issue #3 refinement)
- The AI categorize step is only offered expense/income categories, so it was forced to mislabel
  internal money movements (it called credit-card payments "Bank & Merchant Fees" and bank
  transfers "Personal"). Two fixes:
  - `_ai_review_pending` now runs `importer.rescan_transfers` FIRST and skips any row already
    pointed at one of your own bank/card accounts, so detected transfers are never overwritten by
    a rule or the AI. Precedence is now transfers > rules > history > AI. Note reports transfers matched.
  - `ai._categorize_prompt` tells the model that card payments / account transfers are not
    expenses and to return "Uncategorized Expense" for them (so unmatched card payments to
    untracked cards stop landing in a wrong expense bucket).
- Verified live on real data: matched transfers -> the partner account; Capital One/AMEX/Chase
  card payments and bare "Transfer" rows -> Uncategorized Expense (for review) instead of a wrong
  category. test_categorize.py still 15/15.


### 2026-06-23 — Real fix for "shortcut launches blank": Windows data dir moved outside %AppData%
- Root cause finally found. When the desktop shortcut is opened from inside the Claude desktop
  app, the server runs in that app's **MSIX sandbox**, which silently redirects `%LOCALAPPDATA%`
  to a per-package cache (`...\Packages\Claude_*\LocalCache\Local\`). So the default data dir
  (`%LOCALAPPDATA%\ShopBooks`) resolved to a *different, empty* database — the books looked blank
  even though the real data was safe. (Earlier "stale duplicate server" theory was wrong; this is
  AppData virtualization.) Confirmed: a redirected copy existed under the Claude package cache.
- Fix (final): the Windows default in `db._default_data_dir()` is now `%USERPROFILE%\ShopBooks`
  (was `%LOCALAPPDATA%\ShopBooks`) — a location MSIX never redirects — so **every** launch path
  (shortcut, raw uvicorn, sandboxed or not) reads the same database; no env override needed.
  `run.bat` reverted to its simple form (the `SHOPBOOKS_DATA_DIR` env stays test-only). The existing
  `_migrate_old_location` → `_migrate_from(LEGACY_APPDATA)` now auto-carries any old
  `~/AppData/Local/ShopBooks` install forward to the new location (verified by `test_datamigrate.py`).
- Migration/cleanup for this user: books consolidated to `%USERPROFILE%\ShopBooks` (books.db + docs
  + 41 backups + sync_state.json); the stale `%LOCALAPPDATA%\ShopBooks` and the Claude-package
  sandbox copy were deleted, plus 57 leftover temp test DBs swept. Confirmed: relaunch shows real data.

### 2026-06-19 — Sync receipt files between machines (docs-sync)
- Cloud sync now mirrors the **`docs/` folder** alongside `_sync.db`, via a `_sync_docs/` subfolder
  in the cloud folder. `export`/`Sync now` pushes local receipts up (additive, even if the DB is
  unchanged); `import`/`Pull` brings the other machine's receipts down — including a **backfill** when
  the DB is already up to date (so previously-imported phantom receipts get their files). Receipts are
  immutable + uniquely named, so it's a safe additive union by filename.
- `_apply_import` now **repoints document paths** to this machine's docs folder (keeping each file's
  basename), fixing imported rows that carried the other machine's absolute paths.
- Pairs with the earlier `/doc` robustness (no 500 on a missing file). `test_sync.py` covers push,
  pull, path-repoint, and backfill. NOTE: deletes aren't propagated (additive only) — an orphan file
  may linger in `_sync_docs`, harmless since the DB row is gone.

### 2026-06-19 — Receipt hover preview + inline viewing
- Clicking a receipt now opens it **inline in a new tab** (image / PDF / Amazon order text) instead
  of downloading — `/doc` sends the right media type with `Content-Disposition: inline`.
- New `static/receipt-preview.js` (loaded globally): hovering any `data-doc="/doc/<id>"` element shows
  a small floating popup — the image, the Amazon receipt text, or a "PDF — click to open" note. Kind
  is sniffed from the response Content-Type, so templates only add the attribute. Applied to the 📎 on
  account registers and the Receipts page thumbnails. Pure progressive enhancement (click still works).

### 2026-06-19 — macOS launcher (`run-mac.command`)
- Repo-committed, double-clickable macOS launcher (the Mac equivalent of `run.bat`). Resolves its own
  folder so it works wherever cloned; builds the venv on first run; frees port 8765; serves on the real
  default data location (no `SHOPBOOKS_DATA_DIR`, so cloud sync/backups are active); opens the browser.
  Forces `arch -arm64` on Apple Silicon (via `hw.optional.arm64`) so native wheels load even if a
  Rosetta terminal would otherwise run x86_64. README + CLAUDE.md run instructions updated.
- (The earlier `~/Applications/ShopBooks.app` Dock launcher is machine-specific and not in the repo.)

### 2026-06-19 — Per-OS data location + auto-migration
- `db._default_data_dir` is now OS-aware: Windows `%LOCALAPPDATA%\ShopBooks` (unchanged — existing
  PC installs untouched), macOS `~/Library/Application Support/ShopBooks`, Linux `$XDG_DATA_HOME`/
  `~/.local/share/ShopBooks`. Replaces the old Windows-style `~/AppData/Local/ShopBooks` fallback
  that Mac/Linux were using.
- `_migrate_from` (generalizes `_migrate_old_location`): on first launch at the new location, moves a
  legacy dir's **books.db + docs + backups + sync_state.json** forward and repoints stored receipt
  paths. Runs for both the in-repo `data/` and the old `~/AppData/Local/ShopBooks`. No-op once moved;
  never runs under `SHOPBOOKS_DATA_DIR`. Verified live: the Mac's 21-entry books moved cleanly with
  sync lineage intact. `test_datamigrate.py` added; `test_safety.py` migration cases still pass.

### 2026-06-19 — Harden two-machine cloud sync
- **Machine-local settings no longer sync.** `backup_dir` (and `sync_enabled`) are preserved across
  an import, so pulling another computer's books can't overwrite this machine's cloud-folder path.
  (Real bug it caused: a Mac that pulled the PC's books inherited a Windows `backup_dir`, then wrote
  backups to a literal `C:\Users\...` folder under the repo and broke its own sync. Fixed + cleaned up.)
- **Stable cross-machine content hash.** `content_hash` neutralizes those machine-local settings, so
  identical books hash the same on every machine — no more spurious version bumps / cloud writes when
  only `backup_dir` differs.
- **"Pull from cloud now" button** (Settings → Sync) + `sync.pull()`: import on demand, no app restart
  needed (closes the gap where enabling sync mid-session never pulled).
- **Cloud-file download awareness.** Imports validate the cloud copy is a real, downloaded SQLite DB
  (`_readable_db`) and wait/retry for it (`_wait_readable`) — Dropbox/iCloud online-only placeholders
  no longer cause a silent no-op. New `cloud_unavailable` status surfaces a clear banner instead.
- Never clobbers local data on a bad/placeholder source. `test_sync.py` extended (pull, local-setting
  preservation, stable hash / no spurious export, cloud_unavailable + no-clobber, `_readable_db`).

### 2026-06-18 — Fix: cross-import transfers no longer go uncategorized (regression)
- Root cause: the "retroactive transfer matching" rework replaced `pair_transfers` (which had two
  jobs) with `rescan_transfers`, but only kept the pending↔pending pass — it dropped the
  **already-posted** pass. So when the card payment was posted first and the bank statement imported
  later (or vice-versa), the later side never matched the booked transfer: it sat uncategorized and,
  if posted, would **double-count the payment** (overstating expenses + wrong bank balance).
- Fix: `rescan_transfers` now runs a second pass calling `find_posted_transfer` for any pending
  bank/card row not paired in pass 1, pointing it at the other own account (so it auto-skips on post).
  Idempotent (won't re-count an already-pointed row). `test_transfers.py` scenario B passes again;
  full suite green.

### 2026-06-18 — Smart categorization: learn from the user's own history (issue #3)
- New deterministic **history layer** in `importer.py`: `payee_key` (normalize a bank descriptor to
  a stable vendor key — strip store #s/ids/dates), `history_map`/`history_category` (vendor → the
  category this business has used most, from posted income/expense legs; excludes Uncategorized and
  transfers). Works offline, no AI needed.
- Auto-categorize order is now **rules → your history → AI** in both the import (`stage_transactions`)
  and the Review "AI review" flows. History only fills if the learned category is still active.
- AI gets the history as **few-shot**: `ai._categorize_prompt` embeds "how THIS business categorized
  similar vendors before," so Claude matches the owner's habits/chart. New `categorize_model` setting
  (blank = use `ai_model`) to run categorization on a cheaper/faster model (e.g. Haiku).
- Stays suggestions only — nothing posts without confirmation in Review. `test_categorize.py`
  (15 checks: normalization, history map, rules>history>AI precedence, prompt few-shot, model setting).
- NOTE: `test_transfers.py` has one pre-existing failure (cross-import transfer auto-match) unrelated
  to this change — present on main before it; flagged for separate follow-up.

### 2026-06-18 — Click-to-sort columns everywhere (`static/sort.js`)
- New dependency-free `static/sort.js` (loaded globally in `base.html`). Two mechanisms:
  - **Tables**: add `class="sortable"` to a `<table>` and every text column header becomes
    clickable (click toggles asc/desc, ▲/▼ indicator). Type is auto-detected per column —
    money (`$1,234.56`, `(45.00)`), plain numbers, ISO dates (`YYYY-MM-DD`), text — and it reads
    `<input>`/`<select>` values so editable rows (accounts, customers) sort by their field. Skips
    `<tfoot>`, `tr.no-sort`, and empty/action headers. Blanks/em-dashes sort last.
  - **Card lists**: a `[data-sortbar="#listId"]` toolbar with `[data-field]` buttons sorts the
    `[data-sortitem]` children of that list by their `data-<field>` attribute. Used for the
    receipts page (Date / Vendor / Amount / Status), which is cards, not a table.
- Applied to: Review (transactions), registers, invoices + customers, accounts, mileage, rules,
  jobs, time (+ by-category/by-job), job detail, reconcile overview + per-account (dups/period/
  history), dashboard recent activity, settings restore list, and the receipts card list.
- Deliberately NOT applied to hierarchical/total tables (Reports P&L/balance-sheet rollups, the
  reconcile key/value summary, invoice line-items with a totals row, entry forms) where reordering
  rows would break meaning. Pure client-side; degrades to server order if JS is off.
- **Persistent**: the active sort is saved in `localStorage` keyed by page path + table/list index
  and re-applied on load, so it survives the full page reload after posting/skipping/saving a
  transaction (which is a POST→redirect→GET). Each page/register remembers its own column + direction.

### 2026-06-18 — Retroactive transfer matching (bank↔bank too) + "Find transfers" button
- `importer.rescan_transfers(con)` pairs internal transfers across ALL pending rows, not just at
  import time. Matches equal-and-opposite amounts between two of the user's own bank/card accounts
  within 7 days, greedy by nearest date (each row used once), and points each side's category at
  the other account so posting books one transfer (second side auto-skips via the post-once guard).
  Now handles **bank↔bank** (and card↔card), not only bank↔card credit-card payments. Idempotent.
- Wired into: a new **↔ Find transfers** button on /review (retroactively scans the queue),
  `importer.stage_transactions` (replaces the old import-time `pair_transfers`), and the QBO
  `/migrate/transactions` import (migrated rows previously never got paired at all).
- Note on one-sided transfers: a payment only matches when BOTH sides are in the queue. If only
  the bank statement is imported (not the card's), categorize the bank payment directly to the
  card account (or add a rule on the payee, e.g. "CAPITAL ONE CRCARDPMT" → that card).
- Verified with an isolated test (bank↔card, bank↔bank, real-expense-untouched, no self-pairing,
  idempotent re-run, post-once skip) and a dry-run on a copy of the real books (2 genuine
  two-sided transfers matched: $11,111.11 bank transfer + $51.00 Chase payment).

### 2026-06-18 — Fix "blank books on launch" (hardened launcher)
- Root cause was NOT data loss — the live `books.db` stayed full the whole time. A stale/leftover
  server bound to port 8765 (e.g. a dev instance, or one started with `SHOPBOOKS_DATA_DIR` pointing
  at a temp dir) was answering with a fresh 28-account seed and an empty dashboard.
- `run.bat` now kills whatever holds port 8765 (`netstat | findstr ":8765 " → taskkill`) BEFORE
  starting, guaranteeing one clean server on the real books every launch. Removed the fragile
  nested-quote "delayed browser open" line that could abort the script.
- Note for future debugging: one `run.bat` launch shows TWO `python.exe` processes — the `.venv`
  python is a launcher stub that re-execs the real interpreter. That's one server, not a duplicate.

### 2026-06-18 — Reconciliation, Phase 1: balance check (issue #4)
- New `reconciliations` table + `reconcile.py`. Per bank/card account, enter a statement's closing
  date + ending balance; compares to the book balance as-of that date (`ledger.raw_balance`,
  display-signed so it reads like the statement) and reports the difference (0 = reconciled).
- Saves a checkpoint per statement; `reconcile.status` powers a /reconcile overview (book balance,
  last reconciled, in-balance/off flag, activity since). When off, the account page lists that
  period's transactions and flags likely duplicates (same amount within a few days) to find the gap.
- All deterministic; nothing posts to the ledger. "Reconcile" nav link; `test_reconcile.py` (13
  checks: balance compare, as-of, card sign, checkpoint status, duplicate detection).
- Phase 2 (later): per-transaction "cleared" checkboxes (QuickBooks-style); optional AI explanation
  of a discrepancy.

### 2026-06-17 — Match invoices to existing deposits (no ledger entries)
- New `invoices.matched_entry_id` (column migration): links an invoice to a deposit already on the
  books **without owning it**. `invoice_deposit_candidates` finds posted income legs == invoice
  total near the invoice date, unlinked. Match sets status=paid + paid_date from the deposit and
  links it — **posts nothing** (distinct from Record Payment, which owns its `paid_entry_id`).
- Routes: `POST /invoices/{id}/match`, `/invoices/{id}/unmatch` (only unlinks, never deletes the
  deposit), `/invoices/match-all` (auto-links unique matches). `ledger.delete_entry` clears the
  link if the deposit is ever deleted. Invoice view shows candidates / matched state; Invoices
  page has a "Match to deposits" button. Covered by `test_invoice_match.py`.

### 2026-06-17 — Import invoices from QuickBooks (records only)
- `migrate.parse_invoices` reads a QBO Invoice List / Transaction List CSV (tolerant headers:
  Date, No./Num, Customer/Name, Due Date, Amount/Total, Open Balance, Status; skips non-invoice
  rows in a mixed list). `migrate.import_invoices` creates customers (reused) + invoice records
  with a single summary line item, status paid (open balance 0 / "Paid") else sent, deduped on
  invoice number. **Records only — never posts to the ledger** (cash basis; income comes from
  deposit imports, so no double-counting). Route `POST /invoices/import-qbo` + Invoices-page upload.
- Covered by `test_invoice_import.py`. Column mapping to be verified against the owner's real export.

### 2026-06-17 — Hide/reactivate accounts; loaded owner's real 2025 chart of accounts
- Imported the owner's full 2025 P&L chart of accounts (14 income, ~67 expense incl. parents+subs)
  with the 2-level hierarchy; flattened the one 3-level COGS branch; disambiguated duplicate names
  ("Rent Utilities", "Materials - Consumables & Fixturing"); reused existing Office Supplies/Utilities.
- New active/inactive toggle: `POST /accounts/active` (refuses to hide accounts with posted splits
  or active sub-accounts, so reports stay correct), `ledger.accounts_with_balances(include_inactive=)`
  + `active`/`has_history` flags, Accounts-page Hide/Reactivate + show-hidden. Pickers already
  filter `active=1`. Hid 16 unused seed categories (3 with history kept). Covered by `test_deactivate.py`.

### 2026-06-17 — Recategorize a transaction from its matched receipt (relates to #3)
- `ledger.entry_category` / `ledger.set_entry_category`: read and re-point the single income/
  expense leg of a simple 2-sided entry to another account **of the same type** (amounts
  unchanged → stays balanced). Refuses transfers/multi-split and cross-type moves.
- Receipts page: matched receipts show their current category with a **manual dropdown**
  (reversible) and a **🤖 Suggest from receipt** button; a page-level **Recategorize matched
  transactions from their receipts** batches it. AI reads the receipt vendor/items (Amazon `.txt`
  has the itemized list) and picks from the **expense chart of accounts** via `ai.categorize`.
- Why: card lines like `AMAZON MKTPL` categorize weakly; the order receipt's items (e.g. an RTX
  5070 Ti → Tools & Small Equipment) give a far better category. Matching still never auto-edits
  a category — recategorize is an explicit click and fully reversible. Covered by `test_recategorize.py`.

### 2026-06-17 — Auto-attach Amazon orders as receipts (issue #12)
- `importer.parse_amazon_orders` parses the Amazon order-history CSV (newer
  `Retail.OrderHistory.*.csv` and older Order Reports), tolerant header detection, groups item
  rows by Order ID and sums to an order total. Deterministic — no AI.
- `app._ingest_amazon_order` writes an itemized `.txt` receipt to `docs/`, inserts a `documents`
  row (vendor=Amazon), dedupes on order id (sha256), and auto-matches via `receipt_candidates`
  (amount+date). New route `POST /receipts/import-amazon` + a Receipts-page upload.
- Caveat documented in UI/guide: Amazon bills per shipment, so order totals are approximate
  matches — user confirms unmatched ones in the Receipts page. Covered by `test_amazon.py`.
- Verified against the owner's real Business order report (75 orders): order-level total
  (`Order Net Total`) is taken ONCE per order — item-subtotal summing would be wrong when an
  order-level promo applies (e.g. $148.62 charge vs $161.52 item sum). cp1252 decode fallback
  for ™/® in titles.

### 2026-06-16 — Job costing, Phase 2: tag transactions to jobs (issue #9)
- New nullable `entries.job_id` (in SCHEMA + a guarded `_column_migrations` ALTER) tags a whole
  transaction to a job. `ledger.post_entry` takes an optional `job_id`; `ledger.set_entry_job`
  tags retroactively; `ledger.register` rows now carry their job.
- Assign a job: on the +Entry page, or inline per-row on any account register (auto-submits).
- `timetracking.job_financials` / `job_transactions` compute income − expenses on tagged
  transactions = **net cash profit per job**; `job_report` adds financials, the tagged-transaction
  list, and **effective $/hour** (net cash ÷ hours logged). Jobs page gains a Net-profit column.
- Owner's own labor is NOT subtracted (cash-basis, one person) — shown alongside as $/hour.
- `test_timetracking.py` extended: job financials, retroactive (un)tagging, untagged txns
  excluded, effective hourly, and the splits-sum-to-zero invariant after posting.

### 2026-06-16 — Time tracking & job costing, Phase 1 (issue #9)
- New `jobs` and `time_entries` tables + `default_hourly_rate` setting. Manual time entry only
  (no timer), logged against optional **jobs** (which can link to a customer) and free-text work
  **categories**, with a billable flag + optional per-entry rate.
- `timetracking.py`: hours/billable-value rollups by job and category, per-job report, jobs
  overview (mirrors `insights.py` style). All money in integer cents.
- Pages: `/time` (period totals, entry log, by-category/by-job breakdowns), `/jobs` and
  `/jobs/{id}` (create jobs, mark done, per-job hours + billable value). "Time" nav link.
- **Not posted to the ledger** (managerial, like mileage) and **not wired into invoices** yet —
  billable hours show a dollar value in reports only. `test_timetracking.py` asserts the math and
  that no ledger entries/splits are ever created.
- Phase 2 (separate issues): tag ledger transactions with a `job_id` for full profit-per-job;
  optional invoice-from-billable-time; optional live timer.

### 2026-06-12 — In-app restore, Save button, reset protection (after another data loss)
- The live DB had reset to fresh again (root cause still unconfirmed; suspected an accidental
  recreate). Backups had the data — recovered. Hardening so it can't bite again:
- `backup.snapshot()` now **skips a fresh/seeded DB** (`looks_fresh`), so an accidental reset
  can never evict the good backups via retention (bumped KEEP 20→40).
- `backup.reset_suspected()` → a red banner on every page when the live DB looks empty but a
  data backup exists, linking to Restore.
- One-click **Restore** in Settings (`backup.restore` overwrites via the SQLite backup API,
  stashing a `pre-restore-*` undo copy first; path-traversal guarded).
- **💾 Save button** fixed bottom-left of every screen → snapshots and returns to the page with
  a "Saved ✓" toast.
- Covered by `test_restore.py`; `test_safety.py` updated for skip-when-fresh.

### 2026-06-11 — Sub-accounts (granular chart of accounts)
- `accounts.parent_id` (column migration) enables two-level Category→Subcategory hierarchy;
  sub-accounts inherit the parent's type. Accounts page adds create-sub-account + re-parent
  with validation (same type, parent must be top-level, no 3rd level, unique names).
- Reports roll children under their parent with a subtotal and a "(direct)" line for postings
  made straight to the parent (`ledger._account_tree`); category dropdowns show `Parent : Child`
  labels (`app.categories`); CSV/tax exports indent via `app._write_account_section`.
- Two levels chosen to keep roll-up math un-double-counted; multi-level + per-parent unique
  names left as future work. Covered by `test_subaccounts.py`.

### 2026-06-11 — Automatic credit-card-payment (transfer) matching
- The two sides of a CC payment (bank withdrawal + card payment, equal amount, within 7 days)
  are auto-detected by shape (money-out-of-bank ↔ money-in-to-card, direction-enforced so an
  unrelated deposit + same-size charge isn't mis-paired) and auto-categorized as a transfer.
- `_post_staged` is now transfer-aware (`importer.find_posted_transfer`): a transfer books
  exactly once regardless of import order or "Post all" — the second side auto-skips. Review
  labels rows "transfer to …" / "transfer already recorded". `possible_duplicate` window 4→7.
- New `importer.find_pending_partner` / `find_posted_transfer` / `pair_transfers`; covered by
  `test_transfers.py` (both-pending, cross-import, no false-pairing, zero-sum).

### 2026-06-11 — Fix wrong statement years (deterministic year reconciliation)
- Bug: statement lines show only MM/DD, so the model guessed years and emitted e.g. 2028.
- Schema now extracts `statement_end_date`; `importer.reconcile_years` recomputes each year from
  month/day + the closing date (handles Dec→Jan rollover), ignoring the model's year, and never
  allows a future date. Regex fallback runs `importer.clamp_future_dates`.
- Import → Review note now shows the imported date range to sanity-check at a glance.
- Added a "Discard batch" button in Review (deletes a batch's unposted rows, keeps posted ones)
  so a bad import can be thrown away and redone. Covered by `test_years.py`, `test_discard.py`.

### 2026-06-11 — Local AI via Ollama (pluggable backend)
- `ai_backend` setting: **claude** (default) | **ollama** (fully local) | **hybrid** (local
  receipts + categorize, Claude statements). `ai.py` refactored into `_claude_*`/`_ollama_*`
  impls behind per-task dispatch (`_task_backend`); shared prompts/schemas
- Ollama via httpx `/api/chat` with structured outputs + base64 images; Settings has engine
  dropdown, server URL, model, and a "Test Ollama connection" probe (`ollama_status`)
- httpx is now a runtime dependency. Verified live against a real Ollama (llava:13b): wire
  format + structured output work; weaker models misread totals (caught by review/match gates)
  — recommend `qwen2.5vl` for receipts. Covered by `test_ollama.py` (dispatch, no network)

### 2026-06-11 — Receipt folder import + re-check matches
- "Import a whole folder" on Receipts: scans a folder (optional subfolders) for image/PDF
  receipts, reads each with AI, auto-matches to expense transactions; dedupes on content
  (`documents.sha256`, added via the new `db._column_migrations` guarded-ALTER helper)
- "Re-check matches" button rematches unmatched receipts after more statements are imported
- Refactored single/batch upload through shared `_ingest_receipt()`; covered by
  `test_receiptfolder.py`. Clears engineering-debt item #2 (column migrations)

### 2026-06-11 — AI categorize pending (Review)
- "🤖 AI categorize pending" button on Review re-runs categorization (rules first, Claude for
  the rest) over all pending staged rows; suggestions only, nothing posts. Shows when a key is
  set. `_ai_review_pending()` in app.py; covered by `test_aireview.py` (AI monkeypatched, no network)

### 2026-06-11 — Configurable backup folder
- New `backup_dir` setting: users pick the off-machine backup folder in Settings (any
  OneDrive/Dropbox/external path); blank = auto-detect OneDrive (prior behavior)
- `backup.cloud_dir()` honors the setting; `cloud_source()`/`check_writable()` added;
  `status()` enriched (source, count, writable). Saving validates the folder and writes a
  test backup. Still suppressed in test mode. Covered by `test_backupdir.py`

### 2026-06-11 — Data safety overhaul (after a data-loss incident)
- **Incident:** a test-cleanup script (`Remove-Item data/`) deleted the live database; the user
  lost settings + API key (no transactions had been entered). Root cause: tests ran against the
  real DB and data lived inside the repo folder.
- Moved the live data dir out of the repo to `%LOCALAPPDATA%\ShopBooks` (overridable via
  `SHOPBOOKS_DATA_DIR`); `db.init()` auto-migrates a legacy in-repo `data/` over, fixing receipt paths
- New `backup.py`: consistent startup snapshots (SQLite backup API) to `<datadir>/backups/`
  (last 20) + automatic mirror to `<OneDrive>/ShopBooks Backups/`; one-click full ZIP (DB +
  receipts) and "Back up now" in Settings; Settings shows data path + backup status
- Mandatory test isolation via `SHOPBOOKS_DATA_DIR`; cloud mirror suppressed in test mode;
  `test_safety.py` committed as the canonical proof (clears engineering-debt item #1)

### 2026-06-10 — QuickBooks Online migration
- `/migrate` page + `migrate.py`: imports QBO report CSVs — Account List (chart of accounts,
  QBO type → ShopBooks type/kind), Transaction Detail by Account (history staged into Review
  with categories from the Split column; bank/card sign normalization; other-side rows skipped),
  Customers, Mileage (deduped), plus opening-balance posting against Owner's Equity
- Parser handles QBO grouped-report noise (title rows, totals, sub-account names);
  header rows require ≥2 non-empty cells (bugfix: one-cell title rows false-matched)
- Also this date: green dollar favicon (`make_icon.py`), desktop shortcut, repo published
  to github.com/outlierworkshop/shopbooks (private)

### 2026-06-10 — Phase 2: invoicing, email, tax package
- Customers + invoices (auto-numbered INV-####, line items, draft/sent/paid/void, overdue computed)
- Invoice PDFs (fpdf2) and SMTP emailing with PDF attached (Gmail app-password flow)
- Cash-basis payment recording: posts bank debit / income credit, undoable;
  `ledger.delete_entry` now clears `invoices.paid_entry_id` (bugfix found in testing)
- Taxes page: pre-flight checklist + one-ZIP tax package (P&L, balance sheet, transaction
  detail with receipt filenames, mileage, receipt images)
- Settings expanded: business identity, invoice terms, email templates, SMTP

### 2026-06-10 — Phase 1: core accounting
- Double-entry ledger (entries/splits, zero-sum enforced), seeded chart of accounts for
  1 bank + 3 cards + Square/ACH income + Schedule C expense categories
- Statement import: CSV parser (header sniffing, debit/credit or signed-amount columns),
  PDF via pdfplumber text + Claude extraction, regex fallback; review queue; per-batch sign flip
- Rules engine (substring → category, longest wins, learn-from-approval) + AI categorization
- Receipt upload → Claude vision (vendor/date/total) → amount+date matching, auto-match when unique
- Mileage log with configurable rate; P&L, balance sheet, registers, CSV exports
- Duplicate (transfer) detection; dashboard; settings with local secret storage

## Next up (owner-approved direction, not yet scheduled)

- **Recurring invoices** — monthly/weekly templates that auto-create drafts.
- **Square fee splitting** — a payout deposit is net; optionally split gross sales vs
  "Bank & Merchant Fees" at review time.
- **Inbox folder auto-import** — watch `data/inbox/`; statements dropped there import
  themselves and land in Review.
- **AI monthly close summary** — one-paragraph "here's what happened in your business last
  month" + anomalies (new vendors, unusual amounts), shown on the dashboard or emailed.
- ~~Standalone app (not a browser tab)~~ — **SHIPPED**: macOS 2026-07-11 (signed `ShopBooks.app`),
  Windows 2026-07-12 (`ShopBooks-Setup.exe` via CI). See [docs/standalone-app.md](standalone-app.md)
  and the changelog entries.

## Engineering debt (do these opportunistically)

> **Active queue:** the next fixes (incl. the remaining 2026-07-09 code-quality review items #72/#73/#74)
> are tracked in [docs/next-fixes.md](next-fixes.md).

1. **Test suite**: partially done — `test_safety.py` is committed and the `SHOPBOOKS_DATA_DIR`
   isolation pattern is established. Remaining: fold the throwaway flow scripts
   (import/review/post, invoicing, QBO migrate) into a committed pytest suite with a shared
   tmp-dir fixture.
2. **Entry editing**: today you delete + repost; in-place edit of payee/memo/category would
   be friendlier.
3. **Receipt → new entry**: when a receipt has no statement match (cash purchase), offer
   "create entry from this receipt".
4. **Backup health on dashboard**: surface "last cloud backup N days ago" if it goes stale.
5. **Large receipt folders**: AI reads run synchronously; a big folder blocks the request.
   Consider background processing + progress if it becomes painful.

## Ideas parking lot (unvetted)

Email inbox integration (read statements/receipts from a mailbox) · invoice payment links
(Square checkout) · quarterly estimated-tax calculator · multi-year comparison reports ·
attachment of arbitrary documents to entries (contracts, warranties) · read-only phone view.

## Non-goals (owner has not asked; don't build speculatively)

Multi-user/auth, cloud sync, payroll, inventory, accrual accounting, multi-currency,
plugin systems, rewrites in other stacks.
