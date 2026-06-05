# Protect Media Viewer

A Home Assistant custom integration (with a bundled dashboard card) for browsing
UniFi Protect **smart-detection** event recordings — fast.

It fixes the two pain points of HA's built-in Protect media browser:

- **No thumbnail lag.** Thumbnails are fetched from the NVR once, cached on disk,
  and served instantly thereafter. New detections are pre-cached over the Protect
  websocket so the top of the grid is always warm.
- **Filter by detection type.** Person / Vehicle / Animal / Face / License Plate
  filter chips, plus camera and time-range selectors.

Click a thumbnail and the clip plays inline on the dashboard.

## Status

🚧 Under active development.

- [x] **Phase 1** — integration skeleton + config-flow auth + live query
- [x] **Phase 2** — disk cache + HTTP API (`/events`, `/thumb`) + websocket pre-warmer
- [x] **Phase 3** — inline clip playback (`/clip`, range-enabled, LRU-capped)
- [x] **Phase 4** — dashboard card (filter chips, infinite scroll, inline player)
- [x] **Phase 5** — HACS polish (hassfest + HACS + card CI, MIT license, v0.1.0 release)

### HTTP API (Phase 2)

| Endpoint | Returns |
|---|---|
| `GET /api/protect_media_viewer/events` | Paginated JSON of smart detections. Query: `types` (csv of person,vehicle,animal,face,licensePlate), `camera`, `hours` (default 24) or `start`/`end` ISO, `limit` (default 60, max 200), `offset`. Each event includes a signed `thumbnail` URL. |
| `GET /api/protect_media_viewer/thumb/{event_id}` | Cached JPEG (signed-path auth, immutable cache headers). |
| `GET /api/protect_media_viewer/clip/{event_id}` | Cached MP4 of the event window (+2s/-3s roll), served via `FileResponse` with HTTP range support for `<video>` seeking. |

Thumbnails are fetched from the NVR once (~70 ms) then served from disk (~0 ms).
New detections are pre-cached over the Protect websocket as they finish.

## Installation (HACS, once released)

1. HACS → Integrations → ⋮ → Custom repositories → add
   `https://github.com/johnbr/ha-protect-media-viewer` (category: Integration).
2. Install, restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **Protect Media Viewer**.
4. Enter your NVR host and a **local** Protect account (created on the console
   itself, not a Ubiquiti cloud login).

## The dashboard card

The integration auto-registers `custom:protect-media-viewer-card` — no manual
Lovelace resource needed. Add it to any dashboard:

```yaml
type: custom:protect-media-viewer-card
title: Smart Detections      # optional
default_hours: 24            # optional: 1 / 24 / 168 / 720
default_types: [person, vehicle]   # optional: pre-selected filter chips
columns: 180                 # optional: min thumbnail width (px)
page_size: 60                # optional: events fetched per scroll page
height: 70vh                 # optional: height of the scrolling grid area
                             #   (any CSS length; header/filters stay pinned)
# entry: <config_entry_id>   # optional: only needed with multiple NVRs
```

Filter chips (Person / Vehicle / Animal / Face / Plate), a camera selector and a
time-range selector sit above an infinite-scrolling thumbnail grid. Click a
thumbnail to play the clip inline.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install uiprotect
# Backend tests (read UNIFI_HOST / UNIFI_USERNAME / UNIFI_PASSWORD, hit the live NVR):
python scripts/connectivity_test.py   # phase 1: auth + query
python scripts/phase2_test.py         # phase 2: thumbnail cache + filtering
python scripts/phase3_test.py         # phase 3: clip export + cache

# Card test (no NVR needed; uses jsdom):
npm install
npm test
```

The card ships as plain JS (no build step) — the file under
`custom_components/protect_media_viewer/frontend/` is the artifact.

### Deploy into a running Home Assistant (for testing)

```bash
scripts/deploy.sh /path/to/homeassistant/config          # symlink
scripts/deploy.sh /path/to/homeassistant/config --copy   # or copy
```

Then restart HA and add the integration via **Settings → Devices & Services →
Add Integration → Protect Media Viewer**.
