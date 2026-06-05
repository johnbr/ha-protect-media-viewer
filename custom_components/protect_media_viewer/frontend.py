"""Serve and auto-register the dashboard card.

Mirrors the pattern used by integrations like browser_mod: the bundled card JS
is served from a static path and added as a frontend module, so the
``custom:protect-media-viewer-card`` element is available on dashboards without
the user manually adding a Lovelace resource.
"""

from __future__ import annotations

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
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_CARD_URL, str(card_path), cache_headers=False)]
    )
    # Cache-bust on version so upgrades don't serve a stale card.
    add_extra_js_url(hass, f"{_CARD_URL}?v={version}")
    _LOGGER.debug("Registered Protect Media Viewer card at %s", _CARD_URL)
