"""The Protect Media Viewer integration.

Sets up the authenticated NVR connection, the thumbnail/clip disk caches, the
saved-clips store, the HTTP API, the websocket pre-warmer, and the bundled
dashboard card.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import async_register_views
from .cache import ClipCache, ThumbnailCache
from .const import (
    CONF_FLAG_IN_PROTECT,
    CONF_REMOVE_ADMIN_ONLY,
    CONF_SAVE_MIN_FREE_GB,
    CONF_SAVE_POST_ROLL,
    CONF_SAVE_PRE_ROLL,
    CONF_SAVED_DIR,
    CONF_TRASH_DAYS,
    CONF_URL_SECRET,
    CONF_VERIFY_SSL,
    DEFAULT_CLIP_CACHE_MB,
    DEFAULT_FLAG_IN_PROTECT,
    DEFAULT_REMOVE_ADMIN_ONLY,
    DEFAULT_SAVE_MIN_FREE_GB,
    DEFAULT_SAVE_POST_ROLL,
    DEFAULT_SAVE_PRE_ROLL,
    DEFAULT_TRASH_DAYS,
    DOMAIN,
    SAVED_MARKER,
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
from .saved import SavedConfig, SavedStore
from .websocket import async_start_prewarmer

_LOGGER = logging.getLogger(__name__)

type ProtectMediaViewerEntry = ConfigEntry[RuntimeData]

_THUMB_CACHE_MAX_BYTES = 1024 * 1024 * 1024  # 1 GiB; thumbnails are ~30-40 KB each
_PRUNE_INTERVAL = timedelta(hours=6)
_GLOBALS_REGISTERED = f"{DOMAIN}_globals_registered"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the HTTP views and the dashboard card.

    These are process-global and, importantly, have NO dependency on the NVR
    connection — the views resolve runtime data per request (returning 4xx until
    an entry is ready) and the card degrades to a "Failed to load events" state.
    Registering them here, rather than inside async_setup_entry behind
    ``client.connect()``, guarantees the card JS is served and injected into the
    frontend even when the NVR is unreachable or slow at boot (a config entry in
    ConfigEntryNotReady/retry must never strand every dashboard without the
    card, surfacing as a "Configuration error"). Runs exactly once per process.
    """
    if not hass.data.get(_GLOBALS_REGISTERED):
        async_register_views(hass)
        await async_register_frontend(hass, VERSION)
        hass.data[_GLOBALS_REGISTERED] = True
    return True


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

    saved = SavedStore(
        hass,
        client,
        saved_config(hass, entry.options),
        tz=dt_util.get_default_time_zone(),
        fallback_thumbnail=thumbs.async_get,
        meta={"nvr_id": client.nvr_id(), "integration_version": VERSION},
    )
    await saved.async_init()

    runtime = RuntimeData(
        client=client, thumbs=thumbs, clips=clips, url_secret=url_secret, saved=saved
    )
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    # (The HTTP views + dashboard card are registered once in async_setup, so the
    # card is served regardless of whether this NVR connection succeeded.)

    # Realtime pre-warm of new detections.
    ws_unsub = async_start_prewarmer(hass, runtime)
    runtime.ws_unsub = ws_unsub
    entry.async_on_unload(ws_unsub)

    # Periodic LRU prune of both caches, and the saved clips' trash purge.
    # (Saved clips live outside both cache dirs, so the LRU never sees them.)
    async def _prune(_now) -> None:
        await thumbs.async_prune()
        await clips.async_prune()
        await saved.async_maintain()

    entry.async_on_unload(
        async_track_time_interval(hass, _prune, _PRUNE_INTERVAL)
    )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.debug("Connected to UniFi Protect NVR %s", client.nvr_name())
    return True


def saved_config(hass: HomeAssistant, options: dict) -> SavedConfig:
    """Options -> SavedConfig. An empty saved_dir means the default under /config."""
    configured = (options.get(CONF_SAVED_DIR) or "").strip()
    return SavedConfig(
        root=Path(configured or hass.config.path(DOMAIN, "saved")),
        create_marker=not configured,
        marker=SAVED_MARKER,
        pre_roll_s=int(options.get(CONF_SAVE_PRE_ROLL, DEFAULT_SAVE_PRE_ROLL)),
        post_roll_s=int(options.get(CONF_SAVE_POST_ROLL, DEFAULT_SAVE_POST_ROLL)),
        min_free_bytes=int(options.get(CONF_SAVE_MIN_FREE_GB, DEFAULT_SAVE_MIN_FREE_GB)) * 1024**3,
        trash_days=int(options.get(CONF_TRASH_DAYS, DEFAULT_TRASH_DAYS)),
        flag_in_protect=bool(options.get(CONF_FLAG_IN_PROTECT, DEFAULT_FLAG_IN_PROTECT)),
        remove_admin_only=bool(options.get(CONF_REMOVE_ADMIN_ONLY, DEFAULT_REMOVE_ADMIN_ONLY)),
    )


async def _async_options_updated(hass: HomeAssistant, entry: ProtectMediaViewerEntry) -> None:
    """Options changed: reload so the store picks up the new directory/settings."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: ProtectMediaViewerEntry
) -> bool:
    """Unload a config entry."""
    runtime: RuntimeData = entry.runtime_data
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    await runtime.client.close()
    return True
