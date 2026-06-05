"""Clip production for inline playback.

Exports the footage spanning a detection event (plus a little pre/post roll for
context) to an MP4. This is the single place that decides *how* a clip is
produced, so swapping to an HLS/seek strategy later is a one-file change.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from .protect import ProtectClient

_LOGGER = logging.getLogger(__name__)

# Context padding around the detection window.
_PRE_ROLL = timedelta(seconds=2)
_POST_ROLL = timedelta(seconds=3)
# Fallback length when an event has no end time (e.g. instantaneous detections).
_FALLBACK_LEN = timedelta(seconds=6)
# High-resolution channel.
_CHANNEL_INDEX = 0


def make_clip_producer(client: ProtectClient):
    """Return an ``async (event_id, dest) -> bool`` producer bound to a client."""

    async def _produce(event_id: str, dest: Path) -> bool:
        window = await client.get_event_window(event_id)
        if window is None:
            _LOGGER.debug("No window for event %s; cannot export clip", event_id)
            return False

        camera_id, start, end = window
        clip_start = start - _PRE_ROLL
        clip_end = (end or start + _FALLBACK_LEN) + _POST_ROLL

        try:
            ok = await client.export_clip_to(
                camera_id, clip_start, clip_end, dest, channel_index=_CHANNEL_INDEX
            )
        except Exception:  # noqa: BLE001 - surface as a failed export, not a 500
            _LOGGER.exception("Clip export failed for event %s", event_id)
            return False

        if not ok:
            _LOGGER.warning("Empty clip exported for event %s", event_id)
        return ok

    return _produce
