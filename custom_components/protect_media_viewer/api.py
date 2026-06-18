"""HTTP API for the dashboard card.

Endpoints:
  GET /api/protect_media_viewer/events     -> paginated, filtered detection list
  GET /api/protect_media_viewer/cameras    -> camera list
  GET /api/protect_media_viewer/thumb/{id} -> cached JPEG thumbnail
  GET /api/protect_media_viewer/clip/{id}  -> cached MP4 clip

events/cameras require normal HA auth (the card fetches them via callApi). The
media endpoints (thumb/clip) are loaded by the browser in <img>/<video> tags,
which can't send auth headers. Rather than HA signed paths (whose secret is
regenerated on every restart, invalidating cached URLs), we guard them with our
own HMAC token derived from a secret persisted in the config entry. The token is
deterministic, so URLs are stable -> the browser caches images -> and they keep
working across restarts.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime, timedelta

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN, SMART_DETECT_TYPES
from .models import RuntimeData

_LOGGER = logging.getLogger(__name__)

_API_BASE = f"/api/{DOMAIN}"
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 60
_DEFAULT_HOURS = 24


def _make_token(secret: str, kind: str, event_id: str) -> str:
    """Deterministic, stable HMAC token guarding a media URL."""
    return hmac.new(
        secret.encode(), f"{kind}:{event_id}".encode(), hashlib.sha256
    ).hexdigest()


def _check_token(secret: str, kind: str, event_id: str, token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(_make_token(secret, kind, event_id), token)


def _media_url(kind: str, event_id: str, secret: str, entry_id: str | None) -> str:
    token = _make_token(secret, kind, event_id)
    query = f"t={token}"
    if entry_id:
        query += f"&entry={entry_id}"
    return f"{_API_BASE}/{kind}/{event_id}?{query}"


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

        secret = runtime.url_secret
        for ev in events:
            ev["thumbnail"] = _media_url("thumb", ev["id"], secret, entry_id)
            ev["clip"] = _media_url("clip", ev["id"], secret, entry_id)

        return self.json(
            {
                "events": events,
                "count": len(events),
                "offset": offset,
                "limit": limit,
                # A full upstream page means there is likely more to fetch; the
                # card advances offset by `limit` regardless of camera filtering.
                "has_more": raw_count == limit,
            }
        )


class ThumbnailView(HomeAssistantView):
    """Serve a cached JPEG thumbnail for one event (guarded by a media token)."""

    url = f"{_API_BASE}/thumb/{{event_id}}"
    name = f"api:{DOMAIN}:thumb"
    requires_auth = False  # guarded by our own HMAC token, not HA auth

    async def get(self, request: web.Request, event_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        runtime = _get_runtime(hass, request.query.get("entry"))
        if runtime is None:
            return web.Response(status=404)
        if not _check_token(runtime.url_secret, "thumb", event_id, request.query.get("t")):
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
    requires_auth = False  # guarded by our own HMAC token, not HA auth

    async def get(self, request: web.Request, event_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        runtime = _get_runtime(hass, request.query.get("entry"))
        if runtime is None:
            return web.Response(status=404)
        if not _check_token(runtime.url_secret, "clip", event_id, request.query.get("t")):
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
