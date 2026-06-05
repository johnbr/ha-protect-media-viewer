"""Pre-warm the thumbnail cache from the Protect realtime websocket.

When a smart-detection event ends, its thumbnail becomes available. Fetching it
immediately means it's already on disk by the time it scrolls into view at the
top of the grid, so the first view is instant too.
"""

from __future__ import annotations

import logging
from typing import Callable

from uiprotect.data import Event, WSSubscriptionMessage

from homeassistant.core import HomeAssistant, callback

from .const import SMART_DETECT_TYPES
from .models import RuntimeData

_LOGGER = logging.getLogger(__name__)

_SMART_TYPE_SET = set(SMART_DETECT_TYPES)


def async_start_prewarmer(
    hass: HomeAssistant, runtime: RuntimeData
) -> Callable[[], None]:
    """Subscribe to the websocket and warm thumbnails for finished detections."""

    @callback
    def _on_message(msg: WSSubscriptionMessage) -> None:
        obj = msg.new_obj
        if not isinstance(obj, Event):
            return
        # Only finished smart-detection events have a ready thumbnail.
        if obj.end is None or not obj.smart_detect_types:
            return
        if not _SMART_TYPE_SET.intersection(t.value for t in obj.smart_detect_types):
            return
        hass.async_create_background_task(
            _warm(runtime, obj.id), f"protect_media_viewer_warm_{obj.id}"
        )

    return runtime.client.api.subscribe_websocket(_on_message)


async def _warm(runtime: RuntimeData, event_id: str) -> None:
    try:
        if await runtime.thumbs.async_warm(event_id):
            _LOGGER.debug("Pre-warmed thumbnail for %s", event_id)
    except Exception:  # noqa: BLE001 - best-effort, never crash the ws callback
        _LOGGER.debug("Pre-warm failed for %s", event_id, exc_info=True)
