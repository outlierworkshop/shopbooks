# Automatic mileage from your phone (car Bluetooth → Dropbox → ShopBooks)

Your phone already knows when a drive starts and ends: it connects to the car's Bluetooth when you
get in and disconnects when you park. This guide wires that signal into ShopBooks with **no new
hardware, no subscriptions, and no custom app** — a phone automation writes a tiny text file per
event into a Dropbox folder, and ShopBooks' folder watcher turns pairs of events into trips waiting
for one-click approval on the **Mileage** page (with road distance routed automatically).

```
phone connects to car BT ──▶ event file in Dropbox ──▶ ShopBooks watcher pairs events
                                                       ──▶ trip appears on /mileage for approval
```

## One-time ShopBooks setup (2 minutes)

1. Create a subfolder in your synced Dropbox, e.g. `Dropbox\BP Admin\ShopBooks\trips-inbox`.
2. ShopBooks → **Settings → Folder watchers → Trips folder (mileage)** → Browse to that folder, Save.
3. That's it on the desktop side. Events are scanned about once a minute while ShopBooks is running
   (or immediately via Settings → "Scan folders now").

## The event file format

One file per event (any `.txt` name — make it unique with a timestamp), containing one line:

```
connect,2026-07-14T08:32:11,36.1234,-86.5678
```

`connect` or `disconnect`, then ISO local time, then latitude, longitude. A `connect` followed by
the next `disconnect` (within 12 hours) becomes one trip. Sub-5-minute, sub-0.1-mile pairs (phone
reconnecting in the driveway) are ignored automatically.

## Android setup — MacroDroid (recommended, ~15 minutes)

Install **MacroDroid** (free tier is plenty — this needs one macro). Two variants; Variant 2 is the
simplest to set up, Variant 1 has no extra apps.

### Variant 2 (easiest): MacroDroid writes locally + Dropsync uploads

1. Install **Autosync for Dropbox ("Dropsync")** (free). Pair it: local folder
   `Internal storage/ShopBooksTrips` ⇄ Dropbox `/BP Admin/ShopBooks/trips-inbox`, sync method
   **Upload only**, enable instant upload.
2. MacroDroid → new macro **"ShopBooks trip logger"**:
   - **Trigger:** *Connectivity → Bluetooth Event → Device Connected* → pick your car stereo.
     Add a second trigger: *Device Disconnected* → same device.
   - **Actions:**
     1. *Location → Force Location Update* (GPS, so the fix is fresh).
     2. *Files → Write To File*:
        - Folder: `ShopBooksTrips`
        - Filename: `evt_{year}{month_digit}{dayofmonth}_{hour}{minute}{second}.txt`
          (use the magic-text picker so each file is unique)
        - Text (single line, magic text via the {...} picker):
          `{bt_connected,true=connect,false=disconnect},{year}-{month_digit}-{dayofmonth}T{hour}:{minute}:{second},{lat},{long}`
          — if your MacroDroid version lacks a connect/disconnect variable, just make **two macros**
          (one per trigger) with the literal word `connect` / `disconnect` instead.
   - Test: tap the ▶ test button; a file should appear in the folder, then in Dropbox.
3. Permissions MacroDroid will ask for: **Location → Allow all the time**, and exclude MacroDroid
   from battery optimization (Settings → Apps → MacroDroid → Battery → Unrestricted) so triggers
   fire reliably.

### Variant 1 (no extra apps): MacroDroid uploads straight to Dropbox's API

Same trigger and location actions, but instead of *Write To File* use *Web Interactions →
HTTP Request (POST)*:
- URL: `https://content.dropboxapi.com/2/files/upload`
- Headers:
  - `Authorization: Bearer <your token>`
  - `Dropbox-API-Arg: {"path":"/BP Admin/ShopBooks/trips-inbox/evt_{year}{month_digit}{dayofmonth}_{hour}{minute}{second}.txt","mode":"add"}`
  - `Content-Type: application/octet-stream`
- Body: the event line (same magic-text as above).

Token (once): dropbox.com/developers → App console → Create app → Scoped access →
**App folder** or Full Dropbox → Permissions: `files.content.write` → Generate access token.
(Tokens are short-lived by default now — pick "no expiration" if offered, else prefer Variant 2.)

### Tasker (alternative to MacroDroid)

Profile: *State → Net → BT Connected* (your car) → Task: *Location → Get Location v2*, then
*File → Write File* `ShopBooksTrips/evt_%TIMES.txt` with
`connect,%DATE-ish...` — same idea; pair with Dropsync as in Variant 2. Exit task writes the
`disconnect` line.

## iPhone appendix (for a household mix later)

iOS 17+ Shortcuts: **Automation → Bluetooth → your car → Run Immediately** (no confirmation), with a
shortcut that does *Get Current Location* → *Get Text from Input* formatted as the event line →
save/append a file to the trips-inbox (via the Dropbox app's Files integration, or an
`Get Contents of URL` POST to the same Dropbox API as Variant 1). One automation for connect, one
for disconnect.

## How trips show up

- New drives appear on **Mileage → "Trips waiting for approval"** with reverse-geocoded
  start → end, duration, and **routed road miles** (OSRM). If routing was unreachable you'll see
  "(estimated)" — a straight-line ×1.3 approximation; double-check those.
- Edit miles (detours, multi-stop runs read as point-to-point), add a purpose, **Approve** → it
  becomes a normal mileage-log row. **Dismiss** personal drives.
- Multi-stop errands under one Bluetooth session appear as one point-to-point trip — bump the miles
  at approval, or split it into manual entries.

## Accuracy upgrade path (if ever wanted)

An OBD-II telematics dongle (e.g. Bouncie, ~$90 + $9/mo) records odometer-accurate trips from the
car itself with a pollable API — ShopBooks could fetch trips the same way it pulls bank feeds. Not
built; noted here as the known next step if routed point-to-point distance ever falls short.
