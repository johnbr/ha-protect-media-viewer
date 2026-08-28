"""Serve and auto-register the dashboard card.

Delivery is layered to survive the Companion app's real-world failure modes,
each of which used to strand a page session with no defined card element and
therefore a permanent "Configuration error" card:

1. What the frontend loads is a *composed* module: the card source with the
   retry loader appended, served as one body. The card therefore runs
   ``customElements.define()`` during that module's own evaluation — the
   FIRST network round trip — and the loader that follows it finds the
   element already defined and no-ops.

   The card used to be a separate dynamic ``import()`` issued by the loader,
   which cost a second, serial round trip that could not even begin until the
   first response had been parsed and executed. That lost a race it did not
   need to enter: Home Assistant's service worker serves the app shell and
   ``/frontend_latest/*`` CacheFirst (the dashboard boots with zero network)
   while every custom card module is NetworkFirst, and HA waits only 2s for a
   custom element before painting ``hui-error-card`` — whose title, when the
   config carries ``message`` rather than ``error``, is the bare string
   "Configuration error" with nothing on screen naming the cause. Every
   module fetch succeeded in the logs and the loader never reported a
   failure; the card was simply late. Composing removes the extra round trip
   AND the whole failure mode the retry existed for, since one fetch now
   carries both jobs.

   The loader is kept, and the card stays separately addressable at
   ``_CARD_URL``, purely as belt and braces: if the composed body somehow
   evaluated without defining the element, the loader dynamic-imports the
   card with retry/backoff and reports to the integration's ``/log``
   endpoint so it shows up in the HA log.

2. Both files are served with ``Cache-Control: max-age=0,
   stale-while-revalidate`` plus a strong ETag. Every load revalidates, so a
   corrupt cached copy can never wedge a device (the property the
   cache_headers=False fix established) — but a previously cached copy is
   still served instantly when the device is momentarily offline, which is
   exactly the window in which mobile page loads happen.

3. The composed module is registered BOTH via ``add_extra_js_url``
   (browser_mod pattern) and as a *persisted* Lovelace resource (HACS
   pattern, storage mode only). extra_module_url entries are baked into the
   page at render time, so a page fetched while HA is still booting — the
   Companion app reconnects aggressively during restarts — would otherwise
   never reference the card at all. The resource registry lives in
   ``.storage/lovelace_resources`` and is delivered over the websocket when
   the dashboard loads, closing that window. The two registrations use
   distinct query strings so a failed fetch of one cannot poison the other's
   module-map entry — either copy alone defines the card, and both the
   ``customElements.define()`` guard and the loader's own window flag make
   evaluating the second copy a no-op.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_CARD_FILENAME = "protect-media-viewer-card.js"
_LOADER_FILENAME = "protect-media-viewer-loader.js"
_CARD_URL = f"/{DOMAIN}/{_CARD_FILENAME}"
_LOADER_URL = f"/{DOMAIN}/{_LOADER_FILENAME}"
_LOADER_PLACEHOLDER = "__CARD_URL__"

# Always revalidate (background, non-blocking), but keep a stale copy usable
# for 7 days so the module still loads instantly when the device is offline at
# page load. no-cache would NOT work here: it forces blocking revalidation,
# which fails when offline — max-age=0 + SWR is the offline-tolerant spelling.
_CACHE_CONTROL = "max-age=0, stale-while-revalidate=604800"


def _etag_of(body: bytes) -> str:
    """Strong ETag / content fingerprint for a served module."""
    return hashlib.sha256(body).hexdigest()[:32]


def _render_loader(template: str, card_url: str) -> str:
    """Bake the fingerprinted card URL into the loader source."""
    return template.replace(_LOADER_PLACEHOLDER, card_url)


def _compose_loader(card_source: str, loader_template: str, card_url: str) -> str:
    """Concatenate the card ahead of the loader into one served module.

    Order is load-bearing: the card defines the custom element as this module
    evaluates, so the loader that follows finds it already defined and never
    issues the fallback import. Concatenation (rather than an import) is what
    removes the second round trip — see point 1 of the module docstring.

    Safe because neither file is a real ES module in the linking sense: the
    card has no top-level import/export and declares only its own names, and
    the loader is a self-contained IIFE. A guard test asserts both properties.
    """
    return f"{card_source}\n{_render_loader(loader_template, card_url)}"


class ModuleView(HomeAssistantView):
    """Serve one frontend JS module from memory with ETag + SWR caching.

    Unauthenticated because module <script> fetches can't carry auth headers
    (the old static path was unauthenticated too); the body is a public asset.
    """

    requires_auth = False

    def __init__(self, url: str, name: str, body: bytes) -> None:
        self.url = url
        self.name = name
        self._body = body
        self._etag = _etag_of(body)

    async def get(self, request: web.Request) -> web.Response:
        ua = request.headers.get("User-Agent", "-")
        headers = {"Cache-Control": _CACHE_CONTROL, "ETag": f'"{self._etag}"'}
        if self._etag in request.headers.get("If-None-Match", ""):
            _LOGGER.debug("%s: 304 for %s", self.name, ua)
            return web.Response(status=304, headers=headers)
        _LOGGER.debug("%s: 200 (%d bytes) for %s", self.name, len(self._body), ua)
        return web.Response(
            body=self._body,
            content_type="application/javascript",
            charset="utf-8",
            headers=headers,
        )


async def async_register_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve the card, the composed module, and register it with the frontend."""
    base = Path(__file__).parent / "frontend"
    card_source: str = await hass.async_add_executor_job(
        (base / _CARD_FILENAME).read_text
    )
    loader_template: str = await hass.async_add_executor_job(
        (base / _LOADER_FILENAME).read_text
    )
    card_body = card_source.encode()

    # Cache-bust on content, not release version: the ?v= is only a cache key —
    # the views below always serve the current bytes regardless of ?v=.
    # The loader's fingerprint covers the composed body, so editing EITHER file
    # moves it.
    card_url = f"{_CARD_URL}?v={version}-{_etag_of(card_body)[:12]}"
    loader_body = _compose_loader(card_source, loader_template, card_url).encode()
    loader_url = f"{_LOADER_URL}?v={version}-{_etag_of(loader_body)[:12]}"

    hass.http.register_view(ModuleView(_CARD_URL, f"frontend:{DOMAIN}:card", card_body))
    hass.http.register_view(
        ModuleView(_LOADER_URL, f"frontend:{DOMAIN}:loader", loader_body)
    )

    # Distinct query strings on purpose — see module docstring, point 3.
    add_extra_js_url(hass, f"{loader_url}&src=tag")
    await _async_register_lovelace_resource(hass, f"{loader_url}&src=res")
    _LOGGER.debug("Registered Protect Media Viewer loader at %s", loader_url)


async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> None:
    """Persist the loader as a Lovelace resource (storage mode only).

    Best-effort by design: YAML-mode resource collections are read-only (the
    extra_js_url registration still covers those installs), and a failure here
    must never take down integration setup.
    """
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None)
        if resources is None or not hasattr(resources, "async_create_item"):
            _LOGGER.debug(
                "Lovelace resource registry not writable (YAML mode?); "
                "relying on extra_js_url only"
            )
            return

        # async_items() does not lazy-load the collection; this does.
        await resources.async_get_info()

        base_url = url.split("?", 1)[0]
        ours: list[dict[str, Any]] = [
            item
            for item in resources.async_items()
            if str(item.get("url", "")).split("?", 1)[0] == base_url
        ]
        if not ours:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("Registered the card loader as a Lovelace resource")
            return
        if ours[0].get("url") != url:
            await resources.async_update_item(ours[0]["id"], {"url": url})
            _LOGGER.debug("Updated the card loader Lovelace resource URL")
        for stale in ours[1:]:
            await resources.async_delete_item(stale["id"])
    except Exception:
        _LOGGER.exception("Could not register the card loader as a Lovelace resource")
