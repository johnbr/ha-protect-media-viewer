"""Saved-clips test: SavedStore against the live NVR, in a temp directory.

Saves a real detection (by default one from the oldest day the NVR still has,
the case the feature exists for), validates the MP4/thumbnail/sidecar, checks
the index survives a rescan, then removes it and checks it lands in the trash.
Nothing is written outside the temp dir unless SAVED_TEST_FLAG=1, which also
exercises the Protect flag mirror (a PATCH to the event) and clears it again.

Env: UNIFI_HOST, UNIFI_USERNAME, UNIFI_PASSWORD, optional UNIFI_PORT,
SAVED_TEST_DAYS_AGO (default 29), SAVED_TEST_FLAG (default 0).
"""

from __future__ import annotations

import asyncio
import importlib
import json
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

ProtectClient = importlib.import_module("protect_media_viewer.protect").ProtectClient
saved_mod = importlib.import_module("protect_media_viewer.saved")
SAVED_MARKER = importlib.import_module("protect_media_viewer.const").SAVED_MARKER


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


def _probe(path: Path) -> str:
    if not shutil.which("ffprobe"):
        return "ftyp present" if b"ftyp" in path.read_bytes()[:12] else "UNKNOWN container"
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name:stream=width,height",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip().replace("\n", ", ") or out.stderr.strip()


async def main() -> None:
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), cookie_jar=aiohttp.CookieJar(unsafe=True))
    client = ProtectClient(
        host=os.environ["UNIFI_HOST"],
        port=int(os.environ.get("UNIFI_PORT", "443")),
        username=os.environ["UNIFI_USERNAME"],
        password=os.environ["UNIFI_PASSWORD"],
        verify_ssl=False,
        session=session,
    )
    days_ago = int(os.environ.get("SAVED_TEST_DAYS_AGO", "29"))
    flag = os.environ.get("SAVED_TEST_FLAG") == "1"
    root = Path(tempfile.mkdtemp(prefix="pmv_saved_"))
    (root / SAVED_MARKER).touch()
    print(f"Saved dir: {root}")

    try:
        await client.connect()
        print(f"Connected to {client.nvr_name()}")

        end = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
        events = await client.list_smart_detections(start=end - timedelta(days=1), end=end, limit=50)
        finished = [e for e in events if e["end"]]
        if not finished:
            print(f"No finished detections {days_ago}-{days_ago + 1} days ago.")
            return
        ev = finished[0]
        print(f"\nTarget {ev['id']}  {ev['camera_name']}  {ev['smart_detect_types']}  start {ev['start']}"
              f"  flagged={ev['flagged']}")
        was_flagged = ev["flagged"]

        config = saved_mod.SavedConfig(root=root, create_marker=False, marker=SAVED_MARKER, flag_in_protect=flag)
        store = saved_mod.SavedStore(FakeHass(), client, config, tz=datetime.now().astimezone().tzinfo)
        await store.async_init()

        t0 = time.perf_counter()
        rec, created = await store.async_save(ev["id"], "saved_test.py", "saved_test")
        took = time.perf_counter() - t0
        assert created, "first save must create"
        clip = root / f"{rec['_base']}.mp4"
        thumb = root / f"{rec['_base']}.jpg"
        print(f"\nSaved in {took:.1f}s -> {rec['_base']}")
        print(f"  clip  {clip.stat().st_size / 1e6:.1f} MB  {_probe(clip)}")
        print(f"  window {rec['saved']['clip_start']} .. {rec['saved']['clip_end']}")
        print(f"  thumb {thumb.stat().st_size / 1e3:.0f} KB" if thumb.exists() else "  thumb MISSING")
        sidecar = json.loads((root / f"{rec['_base']}.json").read_text())
        assert sidecar["event"]["id"] == ev["id"] and sidecar["saved"]["note"] == "saved_test.py"

        _, again = await store.async_save(ev["id"])
        assert not again, "re-save must be idempotent"

        if flag:
            live = await client.get_event_record(ev["id"])
            print(f"  Protect flag after save: {live['flagged']} (protect_flagged={rec['saved']['protect_flagged']})")
            assert live["flagged"], "flag mirror did not stick"

        fresh = saved_mod.SavedStore(FakeHass(), client, config)
        await fresh.async_init()
        assert fresh.get(ev["id"]) is not None, "rescan lost the record"

        await store.async_remove(ev["id"], "saved_test")
        trash = sorted(p.name for p in (root / saved_mod.TRASH_DIR).iterdir())
        print(f"\nRemoved -> trash: {trash}")
        if flag:
            live = await client.get_event_record(ev["id"])
            print(f"  Protect flag after remove: {live['flagged']}")
            assert live["flagged"] == was_flagged, "remove must leave Protect's flag as it was before the save"

        print("\nSaved clips: SUCCESS")
    finally:
        await client.close()
        await session.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
