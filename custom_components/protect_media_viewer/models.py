"""Shared runtime types (kept separate to avoid import cycles)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .cache import ClipCache, ThumbnailCache
from .protect import ProtectClient


@dataclass(slots=True)
class RuntimeData:
    """Everything the HTTP views and pre-warmer need at runtime."""

    client: ProtectClient
    thumbs: ThumbnailCache
    clips: ClipCache
    # Persisted secret (from the config entry) used to sign stable media tokens.
    # Unlike HA's signed paths, this survives restarts, so thumbnail/clip URLs
    # stay valid and browser-cacheable across restarts.
    url_secret: str = ""
    ws_unsub: object | None = None
