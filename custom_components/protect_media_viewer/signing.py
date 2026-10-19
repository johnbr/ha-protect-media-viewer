"""Media URLs for the card: Home Assistant signed paths, reused while fresh.

The thumbnail and clip views require Home Assistant auth. A browser loads them
in ``<img>``/``<video>`` tags, which cannot send an auth header, so the event
lists hand out URLs signed with ``async_sign_path``: each one names the user it
was signed for and expires. Anyone who sees a URL (browser history, a proxy
log, a screenshot) has it only until then, and a revoked login takes its URLs
with it.

A signed path embeds the moment it was signed, so signing afresh on every list
request would give every thumbnail a new URL each time a dashboard opens, and
the browser could never reuse what it already downloaded. So a signed URL is
reused, per user, until half its lifetime has gone — every URL handed out still
has at least half of it left.

Pure, with the signing function injected, so it is tested without Home
Assistant.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import urlencode

API_BASE = "/api/protect_media_viewer"

# How long a media URL works. Long enough for a dashboard left open through an
# evening; short enough that a leaked URL is not a standing pass to the footage.
# A Home Assistant restart also ends every signed URL early; the card fetches
# fresh ones when a load is refused.
MEDIA_URL_EXPIRY = timedelta(hours=24)

# Bounds the cache: one entry per (user, media file) a list has shown.
_MAX_CACHED = 20_000

# The media kinds, by view path segment.
LIVE_KINDS = ("thumb", "clip")
SAVED_KINDS = ("saved-thumb", "saved-clip")

Signer = Callable[[str, timedelta, str | None], str]
"""``(path, expiration, refresh_token_id) -> signed path`` — async_sign_path."""


def media_path(kind: str, event_id: str, entry_id: str | None, *, download: bool = False) -> str:
    """The unsigned path; every query parameter on it is covered by the signature."""
    params: list[tuple[str, str]] = []
    if entry_id:
        params.append(("entry", entry_id))
    if download:
        params.append(("download", "1"))
    path = f"{API_BASE}/{kind}/{event_id}"
    return f"{path}?{urlencode(params)}" if params else path


class SignedUrls:
    """Sign media paths, handing back the same URL while it is still fresh."""

    def __init__(
        self,
        sign: Signer,
        expiry: timedelta = MEDIA_URL_EXPIRY,
        clock: Callable[[], float] = time.monotonic,
        max_cached: int = _MAX_CACHED,
    ) -> None:
        self._sign = sign
        self._expiry = expiry
        self._reuse_for = expiry.total_seconds() / 2
        self._clock = clock
        self._max = max_cached
        self._cache: OrderedDict[tuple[str | None, str], tuple[str, float]] = OrderedDict()

    def url(self, path: str, refresh_token_id: str | None) -> str:
        """A signed URL for ``path``, valid for at least half the expiry."""
        key = (refresh_token_id, path)
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[1] < self._reuse_for:
            self._cache.move_to_end(key)
            return hit[0]
        signed = self._sign(path, self._expiry, refresh_token_id)
        self._cache[key] = (signed, now)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return signed

    def clear(self) -> None:
        """Forget every URL — they all die with a restart anyway."""
        self._cache.clear()

    def event_media(
        self, event_id: str, entry_id: str | None, refresh_token_id: str | None, *, saved: bool, has_saved_copy: bool
    ) -> dict[str, str]:
        """``thumbnail``/``clip``, plus ``download`` when there is a saved copy.

        ``saved`` picks where the media is served from: the saved directory
        (the Saved view) or the NVR via the caches (the live view). A live
        event that also has a saved copy downloads that copy.
        """
        thumb_kind, clip_kind = SAVED_KINDS if saved else LIVE_KINDS
        media = {
            "thumbnail": self.url(media_path(thumb_kind, event_id, entry_id), refresh_token_id),
            "clip": self.url(media_path(clip_kind, event_id, entry_id), refresh_token_id),
        }
        if saved or has_saved_copy:
            media["download"] = self.url(
                media_path("saved-clip", event_id, entry_id, download=True), refresh_token_id
            )
        return media
