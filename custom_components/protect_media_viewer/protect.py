"""Thin async wrapper around the uiprotect client.

Centralises connection + the handful of calls the viewer needs so config-flow
validation and runtime share one code path.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from uiprotect import ProtectApiClient
from uiprotect.data import EventType, SmartDetectObjectType, create_from_unifi_dict
from uiprotect.exceptions import ClientError, NotAuthorized

from .recognition import recognitions

_LOGGER = logging.getLogger(__name__)


async def async_create_session(hass: Any, verify_ssl: bool) -> aiohttp.ClientSession:
    """Build a private aiohttp session for talking to the NVR.

    We own this session (not one of HA's shared/managed sessions) so we can
    close it on unload without tripping HA's "closes the Home Assistant aiohttp
    session" warning. The cookie jar must be ``unsafe`` so auth cookies are
    stored for IP-address hosts. The SSL context is built off-loop to avoid a
    blocking-call warning.
    """
    if verify_ssl:
        ssl_context: ssl.SSLContext | bool = await hass.async_add_executor_job(
            ssl.create_default_context
        )
    else:
        ssl_context = False
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    return aiohttp.ClientSession(
        connector=connector, cookie_jar=aiohttp.CookieJar(unsafe=True)
    )

# Event types that carry smart detections. SMART_DETECT_LINE (line-crossing) is
# newer; tolerate uiprotect versions that predate it.
_SMART_EVENT_TYPES = [EventType.SMART_DETECT]
if (_line := getattr(EventType, "SMART_DETECT_LINE", None)) is not None:
    _SMART_EVENT_TYPES.append(_line)


class ProtectAuthError(Exception):
    """Authentication failed (bad username/password/API key)."""


class ProtectConnectionError(Exception):
    """Could not reach or talk to the NVR."""


@dataclass(slots=True)
class CameraInfo:
    """Minimal camera descriptor for the UI."""

    id: str
    name: str


class ProtectClient:
    """Owns a ProtectApiClient and exposes the viewer's data needs."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        verify_ssl: bool,
        session: Any,
    ) -> None:
        self._api = ProtectApiClient(
            host=host,
            port=port,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
            session=session,
            store_sessions=False,
        )

    @property
    def api(self) -> ProtectApiClient:
        return self._api

    async def connect(self) -> None:
        """Authenticate and load the bootstrap, mapping errors to our types."""
        try:
            await self._api.update()
        except NotAuthorized as err:
            raise ProtectAuthError(str(err)) from err
        except (ClientError, OSError) as err:
            raise ProtectConnectionError(str(err)) from err

    async def close(self) -> None:
        await self._api.close_session()

    def nvr_name(self) -> str:
        return self._api.bootstrap.nvr.name

    def nvr_id(self) -> str:
        return self._api.bootstrap.nvr.id

    def cameras(self) -> list[CameraInfo]:
        return [
            CameraInfo(id=cam.id, name=cam.name or cam.id)
            for cam in self._api.bootstrap.cameras.values()
        ]

    def camera_name(self, camera_id: str) -> str:
        cam = self._api.bootstrap.cameras.get(camera_id)
        return cam.name if cam and cam.name else camera_id

    async def list_smart_detections(
        self,
        *,
        start: datetime,
        end: datetime,
        smart_detect_types: list[str] | None = None,
        camera_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return smart-detection events as plain dicts for the API layer."""
        types = None
        if smart_detect_types:
            parsed = []
            for t in smart_detect_types:
                try:
                    parsed.append(SmartDetectObjectType(t))
                except ValueError:
                    _LOGGER.warning("Unknown smart-detect type %r; ignoring", t)
            types = parsed or None

        query: dict[str, Any] = {
            "start": start,
            "end": end,
            "limit": limit,
            "offset": offset,
            "types": _SMART_EVENT_TYPES,
            "smart_detect_types": types,
            "sorting": "desc",
        }
        # The parsed ``get_events`` was always ``get_events_raw`` plus
        # ``create_from_unifi_dict`` per row, and uiprotect 23 (HA 2026.10)
        # dropped it, so do that ourselves. Parsing the raw rows here also keeps
        # fields the Event model doesn't carry (``isFavorite``). Releases old
        # enough to lack ``get_events_raw`` keep the original call.
        rows: list[tuple[Any, dict[str, Any]]]
        if (get_events_raw := getattr(self._api, "get_events_raw", None)) is not None:
            raw = await get_events_raw(**query)
            rows = [(create_from_unifi_dict(row, api=self._api), row) for row in raw]
        else:
            rows = [(ev, {}) for ev in await self._api.get_events(**query)]

        return [
            self._event_dict(ev, row)
            for ev, row in rows
            if not camera_id or ev.camera_id == camera_id
        ]

    def _event_dict(self, ev: Any, raw: dict[str, Any]) -> dict[str, Any]:
        """One event as the plain dict the API layer (and saved sidecars) use."""
        return {
            "id": ev.id,
            "camera_id": ev.camera_id,
            "camera_name": self.camera_name(ev.camera_id),
            "start": ev.start.isoformat() if ev.start else None,
            "end": ev.end.isoformat() if ev.end else None,
            "score": ev.score,
            "smart_detect_types": [t.value for t in ev.smart_detect_types],
            # Protect's own "flag" on the event (UFP 6+). Not a retention
            # guarantee; see saved.py.
            "flagged": bool(raw.get("isFavorite")),
            # Face names / plates Protect matched, best first (see
            # recognition.py). Empty for events with no face or plate.
            "recognition": recognitions(ev),
        }

    async def get_event_record(self, event_id: str) -> dict[str, Any] | None:
        """One event as an API dict, or None when the NVR no longer has it."""
        raw = await self._api.api_request(f"events/{event_id}", raise_exception=False)
        if not isinstance(raw, dict) or not raw.get("start"):
            return None
        raw = await self._listed_row(raw) or raw
        return self._event_dict(create_from_unifi_dict(raw, api=self._api), raw)

    async def _listed_row(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        """The same event as the events LIST returns it, or None.

        ``events/{id}`` leaves the recognition ``group`` off every detection
        crop, with or without ``withoutDescriptions=false``; only the list
        attaches it (measured 2026-10-14, Protect 7.3.70). Without it a saved
        clip would lose its plates and keep faces only as "unclear", so the
        row is re-read over the event's own time span. Best-effort: any
        failure falls back to the single row.
        """
        get_events_raw = getattr(self._api, "get_events_raw", None)
        start_ms = raw.get("start")
        if get_events_raw is None or not isinstance(start_ms, (int, float)):
            return None
        end_ms = raw.get("end") if isinstance(raw.get("end"), (int, float)) else start_ms
        try:
            rows = await get_events_raw(
                start=datetime.fromtimestamp(start_ms / 1000 - 1, UTC),
                end=datetime.fromtimestamp(end_ms / 1000 + 1, UTC),
                limit=100,
            )
        except Exception as err:  # the single row is still usable
            _LOGGER.debug("Could not re-read event %s from the events list: %s", raw.get("id"), err)
            return None
        return next((row for row in rows or [] if row.get("id") == raw.get("id")), None)

    async def set_flagged(self, event_id: str, flagged: bool) -> bool:
        """Set or clear Protect's flag (``isFavorite``) on an event.

        Events aren't writable models (``PATCH events/{id}`` is not a route);
        the Protect web app uses ``POST favorite-events/{id}`` and
        ``POST unfavorite-events/{id}``, which answer with an empty body (hence
        the raw request). Flagging an already-flagged event is a 400, and
        unflagging an unflagged one a 404.

        Best-effort by design: the caller's own action must never fail because
        the account lacks the permission or the NVR refused the write.
        """
        route = "favorite-events" if flagged else "unfavorite-events"
        try:
            await self._api.api_request_raw(f"{route}/{event_id}", method="post")
        except Exception as err:
            _LOGGER.warning("Could not %s event %s in Protect: %s", "flag" if flagged else "unflag", event_id, err)
            return False
        return True

    async def get_thumbnail(self, event_id: str, width: int | None = None) -> bytes | None:
        """Fetch an event thumbnail (JPEG bytes), or None if unavailable.

        We hit the ``events/{id}/thumbnail`` endpoint directly instead of using
        ``get_event_thumbnail``: in uiprotect 10.5.0 (pinned by HA's official
        UniFi Protect integration) that method does ``thumbnail_id.replace("e-",
        "")``, which strips *every* "e-" substring — corrupting any event id that
        contains "e-" (~a quarter of UUIDs) and yielding a 404. A short retry
        covers thumbnails that aren't generated the instant an event ends.
        """
        params: dict[str, Any] = {"w": width} if width else {}
        for attempt in range(2):
            try:
                data = await self._api.api_request_raw(
                    f"events/{event_id}/thumbnail",
                    params=params,
                    raise_exception=False,
                )
            except Exception:
                data = None
            if data:
                return data
            if attempt == 0:
                await asyncio.sleep(0.5)
        return None

    async def get_event_window(
        self, event_id: str
    ) -> tuple[str, datetime, datetime | None] | None:
        """Return (camera_id, start, end) for an event, or None if unknown."""
        ev = await self._api.get_event(event_id)
        if ev is None or ev.start is None:
            return None
        return ev.camera_id, ev.start, ev.end

    async def export_clip_to(
        self,
        camera_id: str,
        start: datetime,
        end: datetime,
        dest: Path,
        channel_index: int = 0,
    ) -> bool:
        """Export the camera footage for [start, end] straight to ``dest``.

        Streams to file via uiprotect's ``output_file`` so a large MP4 never sits
        fully in memory. Returns True if a non-empty file was written.
        """
        await self._api.get_camera_video(
            camera_id,
            start,
            end,
            channel_index=channel_index,
            output_file=dest,
        )
        try:
            return dest.stat().st_size > 0
        except FileNotFoundError:
            return False
