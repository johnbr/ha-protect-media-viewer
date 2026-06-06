# Protect Media Viewer

A Home Assistant custom integration — with a bundled dashboard card — for
browsing UniFi Protect **smart-detection** event recordings quickly.

It fixes the main pain points of HA's built-in Protect media browser:

- **No thumbnail lag.** Each thumbnail is fetched from the NVR once, cached on
  disk, and served instantly afterwards. New detections are pre-cached over the
  Protect websocket as they finish, so the top of the grid is always warm.
- **Filter by detection type.** Person / Vehicle / Animal / Face / License Plate
  filter chips, plus a camera selector and a time-range selector.
- **Inline playback.** Click a thumbnail to play the event clip right on the
  dashboard.

It runs alongside the official UniFi Protect integration — it connects to your
NVR independently and doesn't change anything about your existing setup.

## Requirements

- Home Assistant 2024.12 or newer.
- A **local** UniFi Protect account (created on the console itself, not a
  Ubiquiti cloud login).

## Installation (HACS)

1. In HACS, open the **⋮** menu → **Custom repositories**, and add
   `https://github.com/johnbr/ha-protect-media-viewer` with category
   **Integration**.
2. Search for **Protect Media Viewer**, download it, and restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration → Protect Media
   Viewer** and enter:
   - **Host** (and port, default 443) of your Protect console.
   - A **local** Protect **username / password**.
   - **Verify SSL** — leave unchecked if your console uses a self-signed
     certificate.

The card (`custom:protect-media-viewer-card`) is registered automatically — no
manual Lovelace resource is needed.

## The dashboard card

Add it to any dashboard:

```yaml
type: custom:protect-media-viewer-card
title: Smart Detections            # optional
default_hours: 24                  # optional: 1 / 24 / 168 / 720
default_types: [person, vehicle]   # optional: filter chips selected on load
columns: 180                       # optional: min thumbnail width, px
page_size: 60                      # optional: events fetched per scroll page
height: 70vh                       # optional: height of the scrolling grid area
                                   #   (any CSS length; header/filters stay pinned)
# entry: <config_entry_id>         # optional: only needed with multiple NVRs
```

The title, filter chips (Person / Vehicle / Animal / Face / Plate), camera
selector and time-range selector stay pinned at the top while the thumbnail grid
scrolls and loads more results automatically. Click a thumbnail to play its clip
inline.

## How it works

The integration exposes a small HTTP API the card talks to:

| Endpoint | Purpose |
|---|---|
| `GET /api/protect_media_viewer/events` | Paginated, filtered detection list (JSON). |
| `GET /api/protect_media_viewer/cameras` | Camera list for the selector. |
| `GET /api/protect_media_viewer/thumb/{event_id}` | Cached JPEG thumbnail. |
| `GET /api/protect_media_viewer/clip/{event_id}` | Cached MP4 of the event window (2s before / 3s after), with HTTP range support for seeking. |

Thumbnails and clips are cached on disk under the HA config directory
(`protect_media_viewer/`) and pruned with an LRU size cap. The `thumb` and `clip`
URLs are guarded by a stable per-event token derived from a secret stored in the
config entry, so they can be loaded directly in `<img>` / `<video>` tags, are
cacheable by the browser, and keep working across restarts.

## Development

Backend checks run against a live NVR using the `UNIFI_HOST`, `UNIFI_USERNAME`
and `UNIFI_PASSWORD` environment variables:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install uiprotect
python scripts/connectivity_test.py   # auth + smart-detection query
python scripts/cache_test.py          # thumbnail cache + type filtering
python scripts/clip_test.py           # clip export + cache
```

Import-check the integration against a real Home Assistant install (catches
wrong import paths that a syntax check won't):

```bash
pip install homeassistant uiprotect
python scripts/import_check.py
```

> Home Assistant pins a specific `uiprotect` version via its official UniFi
> Protect integration. Test thumbnail behaviour against **that** version, not
> just the latest — they have differed in ways that matter (see the project
> history).

The card ships as plain JavaScript (no build step); the file under
`custom_components/protect_media_viewer/frontend/` is the artifact. Its smoke
test uses jsdom and needs no NVR:

```bash
npm install
npm test
```

To try a working copy in a running Home Assistant:

```bash
scripts/deploy.sh /path/to/homeassistant/config          # symlink
scripts/deploy.sh /path/to/homeassistant/config --copy   # or copy
```

## License

MIT — see [LICENSE](LICENSE).
