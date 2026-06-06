"""Connectivity test: prove a live smart-detection query + thumbnail fetch.

Run: . .venv/bin/activate && python scripts/connectivity_test.py
Reads UNIFI_HOST / UNIFI_USERNAME / UNIFI_PASSWORD from the environment.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from uiprotect import ProtectApiClient
from uiprotect.data import EventType, SmartDetectObjectType

SMART_TYPES = [
    SmartDetectObjectType.PERSON,
    SmartDetectObjectType.VEHICLE,
    SmartDetectObjectType.ANIMAL,
    SmartDetectObjectType.FACE,
    SmartDetectObjectType.LICENSE_PLATE,
]


async def main() -> None:
    host = os.environ["UNIFI_HOST"]
    username = os.environ["UNIFI_USERNAME"]
    password = os.environ["UNIFI_PASSWORD"]
    port = int(os.environ.get("UNIFI_PORT", "443"))

    session = aiohttp.ClientSession()
    client = ProtectApiClient(
        host=host,
        port=port,
        username=username,
        password=password,
        verify_ssl=False,
        session=session,
        store_sessions=False,
    )

    try:
        print(f"Connecting to {host}:{port} as {username} ...")
        await client.update()  # authenticates + loads bootstrap
        nvr = client.bootstrap.nvr
        print(f"  OK  NVR: {nvr.name}  version={nvr.version}")
        print(f"  Cameras: {[c.name for c in client.bootstrap.cameras.values()]}")

        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(hours=24)
        print(f"\nQuerying smart detections {start:%Y-%m-%d %H:%M} .. {end:%H:%M} UTC")
        events = await client.get_events(
            start=start,
            end=end,
            limit=200,
            types=[EventType.SMART_DETECT, EventType.SMART_DETECT_LINE],
            smart_detect_types=SMART_TYPES,
            sorting="desc",
        )
        print(f"  Got {len(events)} smart-detection events")

        # Breakdown by detection type
        counts: dict[str, int] = {}
        for ev in events:
            for t in ev.smart_detect_types:
                counts[t.value] = counts.get(t.value, 0) + 1
        print(f"  By type: {counts}")

        # Show a few
        cams = client.bootstrap.cameras
        for ev in events[:5]:
            cam = cams.get(ev.camera_id)
            cam_name = cam.name if cam else ev.camera_id
            types = ",".join(t.value for t in ev.smart_detect_types)
            print(f"    {ev.start:%H:%M:%S}  {cam_name:12}  {types:20}  score={ev.score}  id={ev.id}")

        # Prove thumbnail fetch + disk cache
        if events:
            ev = events[0]
            print(f"\nFetching thumbnail for {ev.id} ...")
            data = await client.get_event_thumbnail(ev.id, width=320)
            if data:
                out = Path("scripts/_sample_thumb.jpg")
                out.write_bytes(data)
                print(f"  OK  wrote {len(data)} bytes -> {out}")
            else:
                print("  no thumbnail available yet (event may be in-progress)")

        print("\nConnectivity: SUCCESS")
    finally:
        await client.close_session()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
