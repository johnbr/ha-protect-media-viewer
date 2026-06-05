"""HTTP API for the dashboard card.

Two endpoints:
  GET /api/protect_media_viewer/events    -> paginated, filtered detection list
  GET /api/protect_media_viewer/thumb/{id} -> cached JPEG thumbnail

The events response embeds a *signed* thumbnail URL per event so the card can
drop it straight into an <img> tag (browsers can't attach auth headers to
images; HA's signed-path auth handles it instead).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant

from .const import DOMAIN, SMART_DETECT_TYPES
from .models import RuntimeData

_LOGGER = logging.getLogger(__name__)

_API_BASE = f"/api/{DOMAIN}"
# Long TTL + server-side reuse keeps signed URLs stable so the browser caches
# the images. Regenerate once a URL is within the renew window of expiring.
_URL_SIGN_TTL = timedelta(days=30)
_URL_RENEW_BEFORE = 2 * 24 * 3600  # seconds: regenerate when <2 days remain
_SIGNED_CACHE_MAX = 20000  # bound the per-NVR signed-URL cache
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 60
_DEFAULT_HOURS = 24


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


def _stable_signed(
    hass: HomeAssistant, runtime: RuntimeData, path: str
) -> str:
    """Return a signed URL for ``path``, reusing a cached one while still valid.

    Stable URLs let the browser cache the images instead of re-fetching on
    every scroll/revisit (async_sign_path embeds a fresh timestamp each call).
    """
    now = int(time.time())
    cached = runtime.signed_urls.get(path)
    if cached is not None and cached[1] - now > _URL_RENEW_BEFORE:
        return cached[0]

    if len(runtime.signed_urls) >= _SIGNED_CACHE_MAX:
        runtime.signed_urls.clear()

    # Bind to HA's content user (not the requesting session) so the URL stays
    # valid across logins/token changes — these are long-lived cacheable media.
    url = async_sign_path(hass, path, _URL_SIGN_TTL, use_content_user=True)
    runtime.signed_urls[path] = (url, now + int(_URL_SIGN_TTL.total_seconds()))
    return url


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
        end = _parse_iso(request.query.get("end")) or datetime.now(tz=timezone.utc)
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
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to query smart detections")
            return self.json_message("Upstream query failed", status_code=502)

        raw_count = len(page)
        events = [e for e in page if e["camera_id"] == camera] if camera else page

        entry_suffix = f"?entry={entry_id}" if entry_id else ""
        for ev in events:
            thumb = f"{_API_BASE}/thumb/{ev['id']}{entry_suffix}"
            clip = f"{_API_BASE}/clip/{ev['id']}{entry_suffix}"
            ev["thumbnail"] = _stable_signed(hass, runtime, thumb)
            ev["clip"] = _stable_signed(hass, runtime, clip)

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
    """Serve a cached JPEG thumbnail for one event."""

    url = f"{_API_BASE}/thumb/{{event_id}}"
    name = f"api:{DOMAIN}:thumb"
    requires_auth = True

    async def get(self, request: web.Request, event_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        runtime = _get_runtime(hass, request.query.get("entry"))
        if runtime is None:
            return web.Response(status=400)

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
    requires_auth = True

    async def get(self, request: web.Request, event_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        runtime = _get_runtime(hass, request.query.get("entry"))
        if runtime is None:
            return web.Response(status=400)

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
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def async_register_views(hass: HomeAssistant) -> None:
    """Register HTTP views once for the whole integration."""
    hass.http.register_view(EventsView())
    hass.http.register_view(CamerasView())
    hass.http.register_view(ThumbnailView())
    hass.http.register_view(ClipView())
