"""The Protect Media Viewer integration.

Phase 1: authenticated connection.
Phase 2: thumbnail disk cache, HTTP API (/events, /thumb), websocket pre-warmer.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval

from .api import async_register_views
from .cache import ClipCache, ThumbnailCache
from .const import (
    CONF_URL_SECRET,
    CONF_VERIFY_SSL,
    DEFAULT_CLIP_CACHE_MB,
    DOMAIN,
    VERSION,
)
from .frontend import async_register_frontend
from .models import RuntimeData
from .playback import make_clip_producer
from .protect import (
    ProtectAuthError,
    ProtectClient,
    ProtectConnectionError,
    async_create_session,
)
from .websocket import async_start_prewarmer

_LOGGER = logging.getLogger(__name__)

type ProtectMediaViewerEntry = ConfigEntry[RuntimeData]

_THUMB_CACHE_MAX_BYTES = 1024 * 1024 * 1024  # 1 GiB; thumbnails are ~30-40 KB each
_PRUNE_INTERVAL = timedelta(hours=6)
_VIEWS_REGISTERED = f"{DOMAIN}_views_registered"


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtectMediaViewerEntry
) -> bool:
    """Set up Protect Media Viewer from a config entry."""
    session = await async_create_session(hass, entry.data[CONF_VERIFY_SSL])
    client = ProtectClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        verify_ssl=entry.data[CONF_VERIFY_SSL],
        session=session,
    )

    try:
        await client.connect()
    except ProtectAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ProtectConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    thumbs = ThumbnailCache(hass, client.get_thumbnail, _THUMB_CACHE_MAX_BYTES)
    await thumbs.async_init()

    clips = ClipCache(
        hass,
        make_clip_producer(client),
        DEFAULT_CLIP_CACHE_MB * 1024 * 1024,
    )
    await clips.async_init()

    # Persisted secret for signing stable media tokens (survives restarts, unlike
    # HA's per-process signed-path secret). Generate once and store on the entry.
    url_secret = entry.data.get(CONF_URL_SECRET)
    if not url_secret:
        url_secret = secrets.token_hex(32)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_URL_SECRET: url_secret}
        )

    runtime = RuntimeData(
        client=client, thumbs=thumbs, clips=clips, url_secret=url_secret
    )
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    # Views + frontend are global; register exactly once across all entries.
    if not hass.data.get(_VIEWS_REGISTERED):
        async_register_views(hass)
        await async_register_frontend(hass, VERSION)
        hass.data[_VIEWS_REGISTERED] = True

    # Realtime pre-warm of new detections.
    ws_unsub = async_start_prewarmer(hass, runtime)
    runtime.ws_unsub = ws_unsub
    entry.async_on_unload(ws_unsub)

    # Periodic LRU prune of both caches.
    async def _prune(_now) -> None:
        await thumbs.async_prune()
        await clips.async_prune()

    entry.async_on_unload(
        async_track_time_interval(hass, _prune, _PRUNE_INTERVAL)
    )

    _LOGGER.debug("Connected to UniFi Protect NVR %s", client.nvr_name())
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ProtectMediaViewerEntry
) -> bool:
    """Unload a config entry."""
    runtime: RuntimeData = entry.runtime_data
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    await runtime.client.close()
    return True
