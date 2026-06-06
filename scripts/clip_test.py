"""Clip test: ClipCache + clip export against the live NVR.

Exports a real MP4 for a recent finished detection, validates it, and times the
cache miss (export) vs hit (disk). Uses the same fake-hass shim as cache_test.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

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

ClipCache = importlib.import_module("protect_media_viewer.cache").ClipCache
ProtectClient = importlib.import_module("protect_media_viewer.protect").ProtectClient
make_clip_producer = importlib.import_module(
    "protect_media_viewer.playback"
).make_clip_producer


class FakeConfig:
    def __init__(self, base: Path) -> None:
        self._base = base

    def path(self, *parts: str) -> str:
        return str(self._base.joinpath(*parts))


class FakeHass:
    def __init__(self, base: Path) -> None:
        self.config = FakeConfig(base)

    async def async_add_executor_job(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)


def _probe(path: Path) -> str:
    if not shutil.which("ffprobe"):
        # No ffprobe; fall back to checking the MP4 'ftyp' box magic.
        head = path.read_bytes()[:12]
        return "ftyp present" if b"ftyp" in head else "UNKNOWN container"
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,format_name", "-of", "default=nw=1", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip().replace("\n", ", ") or out.stderr.strip()


async def main() -> None:
    session = aiohttp.ClientSession()
    client = ProtectClient(
        host=os.environ["UNIFI_HOST"],
        port=int(os.environ.get("UNIFI_PORT", "443")),
        username=os.environ["UNIFI_USERNAME"],
        password=os.environ["UNIFI_PASSWORD"],
        verify_ssl=False,
        session=session,
    )
    tmp = Path(tempfile.mkdtemp(prefix="pmv_clip_"))
    print(f"Cache dir: {tmp}")

    try:
        await client.connect()
        print(f"Connected to {client.nvr_name()}")

        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(hours=6)
        events = await client.list_smart_detections(start=start, end=end, limit=50)
        finished = [e for e in events if e["end"]]
        if not finished:
            print("No finished events in the last 6h to export.")
            return
        ev = finished[0]
        dur = (
            datetime.fromisoformat(ev["end"]) - datetime.fromisoformat(ev["start"])
        ).total_seconds()
        print(
            f"\nTarget event {ev['id']}\n"
            f"  {ev['camera_name']}  {ev['smart_detect_types']}  ~{dur:.0f}s window"
        )

        cache = ClipCache(FakeHass(tmp), make_clip_producer(client), max_bytes=10**10)
        await cache.async_init()

        t0 = time.perf_counter()
        path = await cache.async_get_path(ev["id"])
        cold = time.perf_counter() - t0
        if path is None:
            print("  Export FAILED (no clip produced)")
            return
        size = path.stat().st_size
        print(f"\nExport (cache miss): {cold:.1f}s  ->  {size/1024:.0f} KB")
        print(f"  Validation: {_probe(path)}")

        t0 = time.perf_counter()
        path2 = await cache.async_get_path(ev["id"])
        warm = time.perf_counter() - t0
        print(f"Cache hit: {warm*1000:.1f} ms (served from disk, range-enabled)")
        assert path2 == path

        print("\nClip export + cache: SUCCESS")
    finally:
        await client.close()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
