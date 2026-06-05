"""On-disk caches for Protect Media Viewer.

Thumbnails are immutable once an event ends, so the cache key is just the event
ID. The first fetch hits the NVR; every fetch after that is a local file read,
which is what makes scrolling smooth. A per-key lock collapses the thundering
herd you get when a grid of <img> tags loads the same new event at once.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# One canonical thumbnail size on disk (the card sizes with CSS). Keeping a
# single size means one file per event and trivial cache keys.
THUMB_WIDTH = 384


class ThumbnailCache:
    """Disk cache of event thumbnails with size-capped LRU pruning."""

    def __init__(
        self,
        hass: HomeAssistant,
        fetch: Callable[[str, int], Awaitable[bytes | None]],
        max_bytes: int,
    ) -> None:
        self._hass = hass
        self._fetch = fetch
        self._max_bytes = max_bytes
        self._dir = Path(hass.config.path(DOMAIN, "thumbs"))
        self._locks: dict[str, asyncio.Lock] = {}

    async def async_init(self) -> None:
        await self._hass.async_add_executor_job(
            lambda: self._dir.mkdir(parents=True, exist_ok=True)
        )

    def _path(self, event_id: str) -> Path:
        # Shard by a 2-char prefix so a single directory never holds 100k files.
        return self._dir / event_id[:2] / f"{event_id}.jpg"

    def has(self, event_id: str) -> bool:
        return self._path(event_id).is_file()

    async def async_get(self, event_id: str) -> bytes | None:
        """Return cached thumbnail bytes, fetching + storing on a miss."""
        path = self._path(event_id)

        cached = await self._hass.async_add_executor_job(_read_if_exists, path)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(event_id, asyncio.Lock())
        async with lock:
            # Another waiter may have populated it while we queued.
            cached = await self._hass.async_add_executor_job(_read_if_exists, path)
            if cached is not None:
                return cached

            data = await self._fetch(event_id, THUMB_WIDTH)
            if not data:
                return None
            if not _is_valid_jpeg(data):
                # Never cache a partial/garbage response — it would be served
                # forever (immutable). Return it so the client can retry later.
                _LOGGER.warning(
                    "Discarding invalid thumbnail for %s (%d bytes); not caching",
                    event_id,
                    len(data),
                )
                return None
            await self._hass.async_add_executor_job(_write_atomic, path, data)
            return data
        # Note: we intentionally leak the per-id lock; ids are bounded by what is
        # actually viewed in a session and pruning the dict adds race surface.

    async def async_warm(self, event_id: str) -> bool:
        """Pre-fetch a thumbnail if not already cached. Returns True on store."""
        if self.has(event_id):
            return False
        return await self.async_get(event_id) is not None

    async def async_prune(self) -> None:
        """Evict least-recently-used files until under the size cap."""
        await self._hass.async_add_executor_job(
            _prune_dir, self._dir, self._max_bytes, "*.jpg"
        )


class ClipCache:
    """Disk cache of exported event MP4s.

    Unlike thumbnails, clips are served via ``web.FileResponse`` (so the browser
    gets HTTP range support for seeking), hence this returns a *path* rather than
    bytes. Producing a clip is slow, so the per-event lock is essential to avoid
    exporting the same window twice when a user double-clicks.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        produce: Callable[[str, Path], Awaitable[bool]],
        max_bytes: int,
    ) -> None:
        self._hass = hass
        self._produce = produce
        self._max_bytes = max_bytes
        self._dir = Path(hass.config.path(DOMAIN, "clips"))
        self._locks: dict[str, asyncio.Lock] = {}

    async def async_init(self) -> None:
        await self._hass.async_add_executor_job(
            lambda: self._dir.mkdir(parents=True, exist_ok=True)
        )

    def _path(self, event_id: str) -> Path:
        return self._dir / event_id[:2] / f"{event_id}.mp4"

    async def async_get_path(self, event_id: str) -> Path | None:
        """Return the path to the cached clip, producing it on a miss."""
        path = self._path(event_id)
        if await self._hass.async_add_executor_job(path.is_file):
            await self._hass.async_add_executor_job(_touch, path)
            return path

        lock = self._locks.setdefault(event_id, asyncio.Lock())
        async with lock:
            if await self._hass.async_add_executor_job(path.is_file):
                return path

            await self._hass.async_add_executor_job(
                lambda: path.parent.mkdir(parents=True, exist_ok=True)
            )
            tmp = path.with_suffix(".part")
            ok = await self._produce(event_id, tmp)
            if not ok:
                await self._hass.async_add_executor_job(_unlink_quiet, tmp)
                return None
            await self._hass.async_add_executor_job(tmp.replace, path)
            return path

    async def async_prune(self) -> None:
        await self._hass.async_add_executor_job(
            _prune_dir, self._dir, self._max_bytes, "*.mp4"
        )


def _is_valid_jpeg(data: bytes) -> bool:
    """Cheap sanity check: starts with the JPEG SOI marker and isn't tiny."""
    return len(data) >= 1000 and data[:2] == b"\xff\xd8"


def _read_if_exists(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _touch(path: Path) -> None:
    """Bump access time so LRU pruning treats recently played clips as hot."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _prune_dir(root: Path, max_bytes: int, pattern: str = "*") -> None:
    if not root.is_dir():
        return
    files = [(p, p.stat()) for p in root.rglob(pattern)]
    total = sum(st.st_size for _, st in files)
    if total <= max_bytes:
        return
    # Oldest access time first.
    files.sort(key=lambda item: item[1].st_atime)
    for path, st in files:
        if total <= max_bytes:
            break
        try:
            path.unlink()
            total -= st.st_size
        except OSError:  # noqa: PERF203 - best-effort eviction
            _LOGGER.debug("Could not evict %s", path)
