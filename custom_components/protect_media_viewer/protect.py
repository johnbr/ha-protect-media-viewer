"""Thin async wrapper around the uiprotect client.

Centralises connection + the handful of calls the viewer needs so config-flow
validation and runtime share one code path. Caching and HTTP serving build on
top of this in later phases.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from uiprotect import ProtectApiClient
from uiprotect.data import EventType, SmartDetectObjectType
from uiprotect.exceptions import ClientError, NotAuthorized

_LOGGER = logging.getLogger(__name__)

# Event types that carry smart detections.
_SMART_EVENT_TYPES = [EventType.SMART_DETECT, EventType.SMART_DETECT_LINE]


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
            types = [SmartDetectObjectType(t) for t in smart_detect_types]

        events = await self._api.get_events(
            start=start,
            end=end,
            limit=limit,
            offset=offset,
            types=_SMART_EVENT_TYPES,
            smart_detect_types=types,
            sorting="desc",
        )

        result: list[dict[str, Any]] = []
        for ev in events:
            if camera_id and ev.camera_id != camera_id:
                continue
            result.append(
                {
                    "id": ev.id,
                    "camera_id": ev.camera_id,
                    "camera_name": self.camera_name(ev.camera_id),
                    "start": ev.start.isoformat() if ev.start else None,
                    "end": ev.end.isoformat() if ev.end else None,
                    "score": ev.score,
                    "smart_detect_types": [t.value for t in ev.smart_detect_types],
                }
            )
        return result

    async def get_thumbnail(self, event_id: str, width: int | None = None) -> bytes | None:
        return await self._api.get_event_thumbnail(event_id, width=width)

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
