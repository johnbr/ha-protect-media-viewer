"""HTTP API for the dashboard card.

Endpoints:
  GET /api/protect_media_viewer/events     -> paginated, filtered detection list
  GET /api/protect_media_viewer/cameras    -> camera list
  GET /api/protect_media_viewer/thumb/{id} -> cached JPEG thumbnail
  GET /api/protect_media_viewer/clip/{id}  -> cached MP4 clip
  GET /api/protect_media_viewer/saved      -> saved clips, same shape as /events
  POST|PATCH|DELETE /api/protect_media_viewer/saved/{id}
                                           -> save / edit note / remove (saved.py)
  GET /api/protect_media_viewer/saved-thumb/{id}, /saved-clip/{id}
                                           -> files from the saved directory

  POST /api/protect_media_viewer/media    -> fresh media URLs for listed events

Every endpoint requires Home Assistant auth except /log. The card fetches the
JSON ones through callApi. The media ones (thumb/clip and the saved files) are
loaded by <img>/<video> tags, which cannot send an auth header, so the lists
hand out Home Assistant signed paths for them: tied to the user, expiring after
a day, and dead after a restart (see signing.py). The card swaps in fresh ones
from /media before they lapse.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from aiohttp import web
from homeassistant.components.http import KEY_HASS_REFRESH_TOKEN_ID, HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN, SMART_DETECT_TYPES
from .models import RuntimeData
from .saved import SaveError, public_info, valid_event_id
from .signing import MEDIA_URL_EXPIRY

_LOGGER = logging.getLogger(__name__)

_API_BASE = f"/api/{DOMAIN}"
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 60
_DEFAULT_HOURS = 24

# Events the card loader may report (see frontend/protect-media-viewer-loader.js).
_CLIENT_LOG_EVENTS = frozenset(
    {"card_load_failed", "card_load_recovered", "card_load_gave_up"}
)
_CLIENT_LOG_MAX_PER_HOUR = 30
_CLIENT_LOG_DETAIL_MAX = 300

# Ids one /media request may refresh: a long scroll, not a whole archive.
_MEDIA_REFRESH_MAX = 1000
# How long the card may use a URL from a list before asking /media again: every
# URL handed out keeps at least half its lifetime (signing.py).
_MEDIA_TTL_S = int(MEDIA_URL_EXPIRY.total_seconds() / 2)

# SaveError code -> HTTP status.
_SAVE_ERROR_STATUS = {
    "invalid_id": 400,
    "not_saved": 404,
    "not_found": 404,
    "forbidden": 403,
    "unavailable": 503,
    "low_space": 507,
    "upstream": 502,
    "export_failed": 502,
}


def _refresh_token_id(request: web.Request) -> str | None:
    """The requesting login, so a signed URL works for that user only."""
    return request.get(KEY_HASS_REFRESH_TOKEN_ID)


def _live_media(runtime: RuntimeData, request: web.Request, event_id: str, entry_id: str | None) -> dict:
    """Signed media URLs for an event in the live (NVR) view."""
    has_copy = bool(runtime.saved and runtime.saved.saved_info(event_id))
    return runtime.media.event_media(
        event_id, entry_id, _refresh_token_id(request), saved=False, has_saved_copy=has_copy
    )


def _saved_media(runtime: RuntimeData, request: web.Request, event_id: str, entry_id: str | None) -> dict:
    """Signed media URLs for a clip in the Saved view, served from the saved dir."""
    return runtime.media.event_media(
        event_id, entry_id, _refresh_token_id(request), saved=True, has_saved_copy=True
    )


def _get_runtime(hass: HomeAssistant, entry_id: str | None) -> RuntimeData | None:
    """Resolve which configured NVR to serve from."""
    store: dict[str, RuntimeData] = hass.data.get(DOMAIN, {})
    if not store:
        return None
    if entry_id:
        return store.get(entry_id)
    if len(store) == 1:
        return next(iter(store.values()))
    return None  # ambiguous: caller must pass ?entry=


def _int_param(request: web.Request, key: str, default: int, maximum: int) -> int:
    raw = request.query.get(key)
    if raw is None:
        return default
    try:
        return max(0, min(maximum, int(raw)))
    except ValueError:
        return default


class EventsView(HomeAssistantView):
    """Paginated, filtered smart-detection list as JSON."""

    url = f"{_API_BASE}/events"
    name = f"api:{DOMAIN}:events"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry_id = request.query.get("entry")
        runtime = _get_runtime(hass, entry_id)
        if runtime is None:
            return self.json_message(
                "No (or ambiguous) Protect Media Viewer config; pass ?entry=",
                status_code=400,
            )

        # Time range: explicit ISO start/end, else last N hours.
        end = _parse_iso(request.query.get("end")) or datetime.now(tz=UTC)
        start = _parse_iso(request.query.get("start"))
        if start is None:
            hours = _int_param(request, "hours", _DEFAULT_HOURS, 24 * 365)
            start = end - timedelta(hours=hours)

        types_raw = request.query.get("types")
        types = None
        if types_raw:
            types = [t for t in types_raw.split(",") if t in SMART_DETECT_TYPES]

        camera = request.query.get("camera") or None
        limit = _int_param(request, "limit", _DEFAULT_LIMIT, _MAX_LIMIT)
        offset = _int_param(request, "offset", 0, 1_000_000)

        try:
            # Type filtering is native; camera filtering is applied here so that
            # `raw_count` reflects the upstream page size and paging stays correct.
            page = await runtime.client.list_smart_detections(
                start=start,
                end=end,
                smart_detect_types=types,
                camera_id=None,
                limit=limit,
                offset=offset,
            )
        except Exception:
            _LOGGER.exception("Failed to query smart detections")
            return self.json_message("Upstream query failed", status_code=502)

        raw_count = len(page)
        events = [e for e in page if e["camera_id"] == camera] if camera else page

        for ev in events:
            # The saved copy, if any: drives the tile badge and the player's
            # Saved/Download controls (``download`` is only set when there is one).
            ev["saved"] = runtime.saved.saved_info(ev["id"]) if runtime.saved else None
            ev.update(_live_media(runtime, request, ev["id"], entry_id))

        return self.json(
            {
                "events": events,
                "count": len(events),
                "offset": offset,
                "limit": limit,
                # A full upstream page means there is likely more to fetch; the
                # card advances offset by `limit` regardless of camera filtering.
                "has_more": raw_count == limit,
                "saving": await _saving_status(runtime, request),
                "media_ttl": _MEDIA_TTL_S,
            }
        )


class ThumbnailView(HomeAssistantView):
    """Serve a cached JPEG thumbnail for one event (HA auth, via a signed path)."""

    url = f"{_API_BASE}/thumb/{{event_id}}"
    name = f"api:{DOMAIN}:thumb"
    requires_auth = True

    async def get(self, request: web.Request, event_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        runtime = _get_runtime(hass, request.query.get("entry"))
        if runtime is None:
            return web.Response(status=404)

        data = await runtime.thumbs.async_get(event_id)
        if not data:
            return web.Response(status=404)

        return web.Response(
            body=data,
            content_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )


class CamerasView(HomeAssistantView):
    """List cameras for the card's camera selector."""

    url = f"{_API_BASE}/cameras"
    name = f"api:{DOMAIN}:cameras"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        runtime = _get_runtime(hass, request.query.get("entry"))
        if runtime is None:
            return self.json_message("No config", status_code=400)
        cams = [{"id": c.id, "name": c.name} for c in runtime.client.cameras()]
        cams.sort(key=lambda c: c["name"].lower())
        return self.json({"cameras": cams})


class ClipView(HomeAssistantView):
    """Serve a cached MP4 of an event window (range-enabled for seeking)."""

    url = f"{_API_BASE}/clip/{{event_id}}"
    name = f"api:{DOMAIN}:clip"
    requires_auth = True  # loaded by <video>, so via a signed path

    async def get(self, request: web.Request, event_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        runtime = _get_runtime(hass, request.query.get("entry"))
        if runtime is None:
            return web.Response(status=404)

        path = await runtime.clips.async_get_path(event_id)
        if path is None:
            return web.Response(status=404)

        # FileResponse implements HTTP range requests, so <video> seeking works.
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "video/mp4",
                "Cache-Control": "private, max-age=86400",
            },
        )


async def _saving_status(runtime: RuntimeData, request: web.Request) -> dict:
    """What the card needs to draw its save controls for THIS user."""
    if runtime.saved is None:
        return {"available": False, "reason": "saving is not set up", "count": 0, "can_remove": False}
    status = await runtime.saved.async_status()
    status["can_remove"] = _can_remove(runtime, request)
    return status


def _can_remove(runtime: RuntimeData, request: web.Request) -> bool:
    if runtime.saved is None:
        return False
    if not runtime.saved.config.remove_admin_only:
        return True
    user = request.get("hass_user")
    return bool(user is not None and user.is_admin)


def _user_name(request: web.Request) -> str | None:
    user = request.get("hass_user")
    return getattr(user, "name", None) if user is not None else None


def _saved_event(rec: dict, runtime: RuntimeData, request: web.Request, entry_id: str | None) -> dict:
    """A saved record in the /events shape, with media served from the saved dir."""
    ev = dict(rec["event"])
    ev["saved"] = public_info(rec)
    ev.update(_saved_media(runtime, request, ev["id"], entry_id))
    return ev


def _save_error(err: SaveError) -> web.Response:
    return web.json_response(
        {"code": err.code, "message": err.message},
        status=_SAVE_ERROR_STATUS.get(err.code, 400),
    )


class SavedListView(HomeAssistantView):
    """Saved clips, filtered and paged like /events."""

    url = f"{_API_BASE}/saved"
    name = f"api:{DOMAIN}:saved"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry_id = request.query.get("entry")
        runtime = _get_runtime(hass, entry_id)
        if runtime is None:
            return self.json_message(
                "No (or ambiguous) Protect Media Viewer config; pass ?entry=",
                status_code=400,
            )

        types_raw = request.query.get("types")
        types = [t for t in types_raw.split(",") if t in SMART_DETECT_TYPES] if types_raw else None
        days = _int_param(request, "days", 0, 365 * 100)
        since = datetime.now(tz=UTC) - timedelta(days=days) if days else None
        limit = _int_param(request, "limit", _DEFAULT_LIMIT, _MAX_LIMIT)
        offset = _int_param(request, "offset", 0, 1_000_000)

        records, total = ([], 0)
        if runtime.saved is not None:
            records, total = runtime.saved.list(
                camera=request.query.get("camera") or None,
                types=types,
                query=_sanitize_client_text(request.query.get("q"), 100),
                since=since,
                offset=offset,
                limit=limit,
            )
        events = [_saved_event(rec, runtime, request, entry_id) for rec in records]
        return self.json(
            {
                "events": events,
                "count": len(events),
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(events) < total,
                "saving": await _saving_status(runtime, request),
                "media_ttl": _MEDIA_TTL_S,
            }
        )


class SavedItemView(HomeAssistantView):
    """Save (POST), edit the note (PATCH) or remove (DELETE) one clip."""

    url = f"{_API_BASE}/saved/{{event_id}}"
    name = f"api:{DOMAIN}:saved_item"
    requires_auth = True

    async def post(self, request: web.Request, event_id: str) -> web.Response:
        runtime, err = self._resolve(request, event_id)
        if err is not None:
            return err
        body = await _json_body(request)
        try:
            # Shielded: an export takes seconds, and a closed dialog or a
            # dropped phone connection must not abandon it half-written.
            rec, created = await asyncio.shield(
                runtime.saved.async_save(event_id, body.get("note", ""), _user_name(request))
            )
        except SaveError as save_err:
            return _save_error(save_err)
        return self.json(
            {
                "created": created,
                "event": _saved_event(rec, runtime, request, request.query.get("entry")),
            },
            status_code=201 if created else 200,
        )

    async def patch(self, request: web.Request, event_id: str) -> web.Response:
        runtime, err = self._resolve(request, event_id)
        if err is not None:
            return err
        body = await _json_body(request)
        try:
            rec = await runtime.saved.async_update_note(event_id, body.get("note", ""))
        except SaveError as save_err:
            return _save_error(save_err)
        return self.json({"event": _saved_event(rec, runtime, request, request.query.get("entry"))})

    async def delete(self, request: web.Request, event_id: str) -> web.Response:
        runtime, err = self._resolve(request, event_id)
        if err is not None:
            return err
        if not _can_remove(runtime, request):
            return _save_error(SaveError("forbidden", "Only an administrator can remove saved clips."))
        try:
            await runtime.saved.async_remove(event_id, _user_name(request))
        except SaveError as save_err:
            return _save_error(save_err)
        return web.Response(status=204)

    def _resolve(self, request: web.Request, event_id: str) -> tuple[RuntimeData | None, web.Response | None]:
        # The id is validated BEFORE anything touches the filesystem.
        if not valid_event_id(event_id):
            return None, _save_error(SaveError("invalid_id", "Invalid event id."))
        runtime = _get_runtime(request.app["hass"], request.query.get("entry"))
        if runtime is None:
            return None, self.json_message("No (or ambiguous) config; pass ?entry=", status_code=400)
        if runtime.saved is None:
            return None, _save_error(SaveError("unavailable", "Saving is not set up."))
        return runtime, None


async def _json_body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class _SavedMediaView(HomeAssistantView):
    """Serve one file of a saved clip (HA auth via a signed path, like /clip)."""

    requires_auth = True
    kind = ""
    ext = ""
    content_type = ""

    async def get(self, request: web.Request, event_id: str) -> web.StreamResponse:
        runtime = _get_runtime(request.app["hass"], request.query.get("entry"))
        if runtime is None or runtime.saved is None:
            return web.Response(status=404)
        path = runtime.saved.media_path(event_id, self.ext)
        if path is None or not await request.app["hass"].async_add_executor_job(path.is_file):
            return web.Response(status=404)
        headers = {"Content-Type": self.content_type, "Cache-Control": "private, max-age=86400"}
        if request.query.get("download"):
            name = runtime.saved.download_name(event_id, self.ext)
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
        # FileResponse implements HTTP range requests, so <video> seeking works.
        return web.FileResponse(path, headers=headers)


class SavedThumbView(_SavedMediaView):
    url = f"{_API_BASE}/saved-thumb/{{event_id}}"
    name = f"api:{DOMAIN}:saved_thumb"
    kind = "saved-thumb"
    ext = ".jpg"
    content_type = "image/jpeg"


class SavedClipView(_SavedMediaView):
    url = f"{_API_BASE}/saved-clip/{{event_id}}"
    name = f"api:{DOMAIN}:saved_clip"
    kind = "saved-clip"
    ext = ".mp4"
    content_type = "video/mp4"


class MediaUrlsView(HomeAssistantView):
    """Fresh signed media URLs for events the card already lists.

    The card calls this before the URLs it holds lapse, after reconnecting
    (a Home Assistant restart ends every signed URL), and when a load fails --
    so it never sends a dead URL to a media view, where the refusal would count
    as a failed login.
    """

    url = f"{_API_BASE}/media"
    name = f"api:{DOMAIN}:media"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        entry_id = request.query.get("entry")
        runtime = _get_runtime(request.app["hass"], entry_id)
        if runtime is None:
            return self.json_message("No (or ambiguous) config; pass ?entry=", status_code=400)
        body = await _json_body(request)
        ids = body.get("ids")
        if not isinstance(ids, list) or len(ids) > _MEDIA_REFRESH_MAX:
            return self.json_message(f"ids must be a list of at most {_MEDIA_REFRESH_MAX}", status_code=400)
        saved = body.get("saved") is True
        build = _saved_media if saved else _live_media
        media = {
            event_id: build(runtime, request, event_id, entry_id)
            for event_id in dict.fromkeys(ids)
            if isinstance(event_id, str) and valid_event_id(event_id)
        }
        return self.json({"media": media, "media_ttl": _MEDIA_TTL_S})


def _sanitize_client_text(value: object, max_len: int) -> str:
    """Reduce client-supplied text to something safe to put in the HA log."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:max_len]


def _format_client_log(event: str, attempts: int, detail: str, user_agent: str) -> str:
    msg = f"Frontend reported {event} (attempts={attempts}, ua={user_agent or '-'})"
    if detail:
        msg += f": {detail}"
    return msg


class _RateLimiter:
    """Global sliding-window limiter for the unauthenticated log endpoint."""

    def __init__(
        self, max_events: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_events
        self._window = window_s
        self._clock = clock
        self._stamps: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._stamps and now - self._stamps[0] > self._window:
            self._stamps.popleft()
        if len(self._stamps) >= self._max:
            return False
        self._stamps.append(now)
        return True


class ClientLogView(HomeAssistantView):
    """Let the card loader surface load failures in the HA log.

    Unauthenticated on purpose: the loader calls it precisely when the page has
    no working hass connection yet (that's what's being diagnosed). Hardened
    instead: fixed event vocabulary, clamped integers, printable+length-capped
    detail text, and a global rate limit.
    """

    url = f"{_API_BASE}/log"
    name = f"api:{DOMAIN}:log"
    requires_auth = False

    def __init__(self) -> None:
        self._limiter = _RateLimiter(_CLIENT_LOG_MAX_PER_HOUR, 3600)

    async def post(self, request: web.Request) -> web.Response:
        if not self._limiter.allow():
            return web.Response(status=429)
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400)
        if not isinstance(payload, dict):
            return web.Response(status=400)

        event = payload.get("event")
        if event not in _CLIENT_LOG_EVENTS:
            return web.Response(status=400)
        attempts = payload.get("attempts")
        if not isinstance(attempts, int) or not 0 <= attempts <= 1000:
            attempts = 0
        detail = _sanitize_client_text(payload.get("detail"), _CLIENT_LOG_DETAIL_MAX)
        ua = _sanitize_client_text(request.headers.get("User-Agent"), 200)

        # WARNING so it is visible without enabling debug logging — this is the
        # trail for the intermittent mobile "Configuration error" reports.
        _LOGGER.warning("%s", _format_client_log(event, attempts, detail, ua))
        return web.Response(status=204)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def async_register_views(hass: HomeAssistant) -> None:
    """Register HTTP views once for the whole integration."""
    hass.http.register_view(EventsView())
    hass.http.register_view(CamerasView())
    hass.http.register_view(ThumbnailView())
    hass.http.register_view(ClipView())
    hass.http.register_view(SavedListView())
    hass.http.register_view(SavedItemView())
    hass.http.register_view(SavedThumbView())
    hass.http.register_view(SavedClipView())
    hass.http.register_view(MediaUrlsView())
    hass.http.register_view(ClientLogView())
