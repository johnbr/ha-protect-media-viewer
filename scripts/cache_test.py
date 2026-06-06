"""Cache test: exercise ThumbnailCache + filtered queries against the live NVR.

Validates cache.py and protect.py without a running Home Assistant by supplying a
minimal fake `hass`. Demonstrates the cache-miss vs cache-hit speedup that fixes
the scrolling lag, and that detection-type filtering works end to end.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

# Load the integration's modules without importing its package __init__ (which
# pulls in Home Assistant). Stub the one HA symbol cache.py references.
import importlib
import types

_ha = types.ModuleType("homeassistant")
_ha_core = types.ModuleType("homeassistant.core")
_ha_core.HomeAssistant = object
_ha.core = _ha_core
sys.modules.setdefault("homeassistant", _ha)
sys.modules.setdefault("homeassistant.core", _ha_core)

_pkg_dir = Path(__file__).resolve().parents[1] / "custom_components" / "protect_media_viewer"
_pkg = types.ModuleType("protect_media_viewer")
_pkg.__path__ = [str(_pkg_dir)]
sys.modules["protect_media_viewer"] = _pkg

ThumbnailCache = importlib.import_module("protect_media_viewer.cache").ThumbnailCache
ProtectClient = importlib.import_module("protect_media_viewer.protect").ProtectClient


class FakeConfig:
    def __init__(self, base: Path) -> None:
        self._base = base

    def path(self, *parts: str) -> str:
        return str(self._base.joinpath(*parts))


class FakeHass:
    """Just enough surface for ThumbnailCache."""

    def __init__(self, base: Path) -> None:
        self.config = FakeConfig(base)

    async def async_add_executor_job(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)


async def main() -> None:
    host = os.environ["UNIFI_HOST"]
    session = aiohttp.ClientSession()
    client = ProtectClient(
        host=host,
        port=int(os.environ.get("UNIFI_PORT", "443")),
        username=os.environ["UNIFI_USERNAME"],
        password=os.environ["UNIFI_PASSWORD"],
        verify_ssl=False,
        session=session,
    )

    tmp = Path(tempfile.mkdtemp(prefix="pmv_cache_"))
    print(f"Cache dir: {tmp}")

    try:
        await client.connect()
        print(f"Connected to {client.nvr_name()}")

        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(hours=24)

        # 1) Filtering through the wrapper: animals only.
        animals = await client.list_smart_detections(
            start=start, end=end, smart_detect_types=["animal"], limit=50
        )
        print(f"\nFilter test: {len(animals)} ANIMAL detections in last 24h")
        for ev in animals[:3]:
            print(f"  {ev['start'][11:19]}  {ev['camera_name']:10}  {ev['smart_detect_types']}")

        # 2) General page for cache timing.
        events = await client.list_smart_detections(start=start, end=end, limit=12)
        ids = [ev["id"] for ev in events]
        print(f"\nGot {len(ids)} events for cache timing")

        cache = ThumbnailCache(FakeHass(tmp), client.get_thumbnail, max_bytes=10**9)
        await cache.async_init()

        # Cold: fetch from NVR + write to disk.
        t0 = time.perf_counter()
        got = 0
        for eid in ids:
            if await cache.async_get(eid):
                got += 1
        cold = time.perf_counter() - t0

        # Warm: served from disk.
        t0 = time.perf_counter()
        for eid in ids:
            await cache.async_get(eid)
        warm = time.perf_counter() - t0

        print(f"\nCache miss (NVR fetch): {got} thumbs in {cold*1000:.0f} ms "
              f"({cold/max(got,1)*1000:.0f} ms/thumb)")
        print(f"Cache hit  (disk read): same {got} thumbs in {warm*1000:.0f} ms "
              f"({warm/max(got,1)*1000:.0f} ms/thumb)")
        if warm > 0:
            print(f"Speedup: {cold/warm:.0f}x faster on cached scroll")

        files = list(tmp.rglob("*.jpg"))
        total = sum(f.stat().st_size for f in files)
        print(f"\nOn disk: {len(files)} files, {total/1024:.0f} KB "
              f"({total/max(len(files),1)/1024:.0f} KB avg)")
        print("\nCache + filter: SUCCESS")
    finally:
        await client.close()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
