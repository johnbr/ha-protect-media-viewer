"""Serve and auto-register the dashboard card.

Mirrors the pattern used by integrations like browser_mod: the bundled card JS
is served from a static path and added as a frontend module, so the
``custom:protect-media-viewer-card`` element is available on dashboards without
the user manually adding a Lovelace resource.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_CARD_FILENAME = "protect-media-viewer-card.js"
_CARD_URL = f"/{DOMAIN}/{_CARD_FILENAME}"


async def async_register_frontend(hass: HomeAssistant, version: str) -> None:
    """Register the static card file and load it on the frontend (once)."""
    card_path = Path(__file__).parent / "frontend" / _CARD_FILENAME
    # Serve the card *revalidating*, NOT cached-without-revalidation. Earlier
    # versions used cache_headers=True (Cache-Control: public, max-age=1 month)
    # to avoid re-downloading the module. That backfired: over that month the
    # mobile/tablet Companion webview would
    # occasionally cache a bad copy under a given ?v= URL — a partial download
    # stored as a complete 200, or a 404 fetched in a window where the path
    # wasn't registered yet — and "immutable" meant it never revalidated, so the
    # module never ran customElements.define() and the dashboard painted a
    # permanent "Configuration error" card. The only escape was minting a new
    # ?v= (a manual cache-bust). cache_headers=False lets aiohttp serve with
    # Last-Modified/ETag, so the webview does a conditional GET each load: a
    # cheap 304 when unchanged, fresh bytes when changed, and crucially it can
    # never get permanently wedged on a broken cached copy. (A ~16 KB module
    # revalidated over the LAN costs nothing.) The content-hash ?v= below stays
    # as belt-and-suspenders so the URL still changes the instant the file does.
    fingerprint = await hass.async_add_executor_job(_card_fingerprint, card_path)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_CARD_URL, str(card_path), cache_headers=False)]
    )
    cache_bust = f"{version}-{fingerprint}" if fingerprint else version
    add_extra_js_url(hass, f"{_CARD_URL}?v={cache_bust}")
    _LOGGER.debug("Registered Protect Media Viewer card at %s?v=%s", _CARD_URL, cache_bust)


def _card_fingerprint(card_path: Path) -> str:
    """Short content hash of the card JS, used as the cache-bust token."""
    try:
        return hashlib.sha256(card_path.read_bytes()).hexdigest()[:12]
    except OSError:
        _LOGGER.warning("Could not read %s to fingerprint the card", card_path)
        return ""
