# UniFi Protect Media Viewer

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
- **Saved clips.** Save a clip from the player and it is copied out of Protect
  (full resolution, the whole event plus padding), so it survives after Protect
  deletes the footage at the end of its retention period. Browse, search,
  download and remove saved clips from the card's **Saved** tab.

It runs alongside the official UniFi Protect integration — it connects to your
NVR independently and doesn't change anything about your existing setup.

## Requirements

- Home Assistant 2024.12 or newer.
- A **local** UniFi Protect account (created on the console itself, not a
  Ubiquiti cloud login).

## Installation (HACS)

The integration is in the HACS default store.

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=johnbr&repository=ha-protect-media-viewer&category=integration)

1. In HACS, search for **UniFi Protect Media Viewer**, download it (or use the
   button above), and restart Home Assistant.
2. Go to **Settings → Devices & Services → Add Integration → Protect Media
   Viewer** and enter:
   - **Host** (and port, default 443) of your Protect console.
   - A **local** Protect **username / password**.
   - **Verify SSL** — leave unchecked if your console uses a self-signed
     certificate.

If your HACS doesn't list it yet (the default store can take a few hours to
refresh), add it as a custom repository instead: HACS → **⋮** → **Custom
repositories** → `https://github.com/johnbr/ha-protect-media-viewer`, category
**Integration**, then continue from step 1.

If you later change that account's password, Home Assistant asks you to sign
in again under **Settings → Devices & Services**: enter the new login there.
Your saved-clip settings are kept.

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
default_mode: detections           # optional: detections | saved (tab shown on load)
show_saved: true                   # optional: false hides the Detections | Saved tabs
allow_save: true                   # optional: false hides the Save button
# entry: <config_entry_id>         # optional: only needed with multiple NVRs
```

The title, filter chips (Person / Vehicle / Animal / Face / Plate), camera
selector and time-range selector stay pinned at the top while the thumbnail grid
scrolls and loads more results automatically. Click a thumbnail to play its clip
inline.

### Face and plate recognition

When Protect recognises a face or reads a plate (UniFi Protect 6+ with face /
license-plate recognition enabled), the tile shows it as a chip with Protect's
match confidence:

- **green**: a face or plate you've named in Protect, e.g. `Johnny 80%` or
  `Roadrunner · 1FSTSLA 89%`;
- **amber**: recognised but not named yet, e.g. `Unknown face 95%` or a plate
  read like `9UYB036 88%`. An unnamed face is not necessarily a stranger. Name
  the face in Protect and later matches turn green.

Faces too blurry or angled to match ("face unclear") get no chip, because that
is most face detections. They still appear, with every other match, in the
tile's tooltip and the player caption.

## Saved clips

Protect deletes recordings once they are older than the console's retention
period, and the event goes with them. Protect's own **flag** does not prevent
that (Ubiquiti documents it only as protection against Enhanced Retention's
resolution downgrade), so the integration keeps its own copy.

- **Save:** open a clip and press **Save** (optionally with a note). The
  integration exports the event at full resolution, from *pre-roll* seconds
  before the detection to *post-roll* seconds after it ends, however long the
  event is. Protect only has footage that it recorded, so a camera recording
  "on detections" yields just the recorded part.
- **Browse:** the **Saved** tab lists saved clips with the same type and camera
  filters, a 30 days / 1 year / all range, and a search box that matches notes,
  camera names and who saved the clip. Saved clips carry a bookmark badge in
  both tabs; events flagged in Protect carry a flag.
- **Download / Remove:** the player offers **Download** and **Edit note** for a
  saved clip, and **Remove** for administrators (configurable). Removing moves
  the files to a `.trash` folder that is emptied after a configurable number of
  days.

Each saved clip is three files, named so they sort by time and read sensibly
in a file manager:

```
<saved folder>/2026/10/2026-10-13_221504_driveway_<event_id>.mp4
                                                    .jpg   thumbnail
                                                    .json  details + note (the record)
```

The JSON sidecars are the only record; there is no database. They are read at
startup and every 6 hours, so clips copied in or deleted by hand are picked up.

### Options

**Settings → Devices & Services → UniFi Protect Media Viewer → Configure**:

| Option | Default | |
|---|---|---|
| Saved clips folder | *(empty)* | Empty keeps clips in `<config>/protect_media_viewer/saved`, which is included in Home Assistant backups. Otherwise an absolute path as Home Assistant sees it, e.g. `/media/protect_saved` |
| Seconds to keep before / after the detection | 10 / 10 | Padding around the whole event |
| Free space to keep in reserve (GB) | 10 | Saving is refused below this |
| Days to keep removed clips | 30 | `.trash` is emptied after this |
| Also flag saved events in Protect | off | Flags the event in Protect on save and clears the flag on remove (only if the integration set it). Needs a Protect account that may edit events |
| Only administrators can remove saved clips | on | Anyone can save |

**A folder you configure must already exist.** When you set it, the integration
writes a marker file, `.protect_media_viewer_saved`, into it. If the folder is
later found without the marker, saving pauses (the card says why) instead of
writing into whatever is underneath. This matters when the folder is a mount:
if a disk or share isn't mounted when Docker starts Home Assistant, Docker
silently creates an empty directory in its place on the system disk.

To keep clips on another disk with Home Assistant in Docker, bind-mount it,
then set the folder to the container path:

```yaml
    volumes:
      - /mnt/bigdisk/protect-saved:/media/protect_saved
```

A folder under `/media` also shows up in Home Assistant's own **Media** panel.

## How it works

The integration exposes a small HTTP API the card talks to:

| Endpoint | Purpose |
|---|---|
| `GET /api/protect_media_viewer/events` | Paginated, filtered detection list (JSON). |
| `GET /api/protect_media_viewer/cameras` | Camera list for the selector. |
| `GET /api/protect_media_viewer/thumb/{event_id}` | Cached JPEG thumbnail. |
| `GET /api/protect_media_viewer/clip/{event_id}` | Cached MP4 of the event window (2s before / 3s after), with HTTP range support for seeking. |
| `GET /api/protect_media_viewer/saved` | Saved clips, same shape as `/events` (`camera`, `types`, `q`, `days`, `offset`, `limit`). |
| `POST` / `PATCH` / `DELETE /api/protect_media_viewer/saved/{event_id}` | Save (`{"note": …}`), edit the note, remove. |
| `GET /api/protect_media_viewer/saved-thumb/{event_id}`, `…/saved-clip/{event_id}` | Files of a saved clip; `?download=1` serves the MP4 as an attachment. |
| `POST /api/protect_media_viewer/media` | Fresh signed media URLs for events the card lists (`{"ids": […], "saved": false}`). |

Thumbnails and clips are cached on disk under the HA config directory
(`protect_media_viewer/`) and pruned with an LRU size cap. Saved clips live
in their own folder and are never touched by that pruning.

Every endpoint requires Home Assistant authentication. The media ones (`thumb`,
`clip` and the saved files) are loaded by `<img>` / `<video>` tags, which can't
send an auth header, so the lists hand them out as Home Assistant **signed
paths**: each URL is tied to the user it was signed for and expires after 24
hours, and a restart ends them all. A URL is reused for its first 12 hours, so
the browser still caches thumbnails between visits. The card fetches fresh URLs
from `/media` before the ones it holds lapse, after reconnecting to a restarted
Home Assistant, and when a thumbnail or clip fails to load.

The card itself is delivered through a small loader module that is registered
both as a frontend module and as a Lovelace resource (you will see one
`protect-media-viewer-loader.js` entry under **Settings → Dashboards →
Resources**; it is recreated on restart if removed). The loader retries the
card import with backoff — mobile page loads routinely race a network gap, and
a browser only ever attempts a module script once — and reports failures to
`POST /api/protect_media_viewer/log`.

## Troubleshooting

**A card shows "Configuration error"** — Home Assistant paints this when the
`protect-media-viewer-card` element was never defined in that page session,
i.e. the card JS failed to load (historically: a network gap during app
startup on mobile, or a page loaded while HA was still booting). The loader
now retries and self-heals these, and every failure/recovery is written to the
HA log at WARNING level, e.g.:

```
Frontend reported card_load_recovered (attempts=4, ua=Home Assistant/…)
```

Check **Settings → System → Logs** and filter for `protect_media_viewer`. If
you see `card_load_gave_up`, the device could not fetch the card JS at all for
~15 minutes — check connectivity/proxy rules for `/protect_media_viewer/*`.

For request-level detail (every serve of the loader/card, 200 vs 304, per
user agent), enable debug logging:

```yaml
logger:
  logs:
    custom_components.protect_media_viewer: debug
```

## Development

Backend checks run against a live NVR using the `UNIFI_HOST`, `UNIFI_USERNAME`
and `UNIFI_PASSWORD` environment variables:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install uiprotect
python scripts/connectivity_test.py   # auth + smart-detection query
python scripts/cache_test.py          # thumbnail cache + type filtering
python scripts/clip_test.py           # clip export + cache
python scripts/saved_test.py          # save / rescan / remove in a temp dir
SAVED_TEST_FLAG=1 python scripts/saved_test.py   # ...and the Protect flag (writes to the NVR, then clears it)
```

The offline unit tests (`pytest tests/`) cover the saved-clips store without
an NVR: sidecars, index rebuild, the marker and free-space refusals, id
validation, the trash and the flag bookkeeping.

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
