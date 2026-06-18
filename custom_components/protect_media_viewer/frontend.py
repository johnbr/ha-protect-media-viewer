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
    # Serve with caching enabled, but cache-bust on the file's *content hash*
    # rather than the release version. The static asset is served immutable, so
    # browsers (especially the mobile/tablet Companion webview, which evicts and
    # re-fetches unpredictably) hold onto a given ?v= copy indefinitely. Busting
    # on `version` alone strands every device on a stale card whenever the JS is
    # edited without a version bump — the card silently fails to register and
    # the dashboard paints a "Configuration error" card until the URL changes.
    # A content fingerprint changes the moment the file does, so a fresh build
    # always fetches fresh while an unchanged build stays fully cached.
    fingerprint = await hass.async_add_executor_job(_card_fingerprint, card_path)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_CARD_URL, str(card_path), cache_headers=True)]
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
