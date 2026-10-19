"""Shared runtime types (kept separate to avoid import cycles)."""

from __future__ import annotations

from dataclasses import dataclass

from .cache import ClipCache, ThumbnailCache
from .protect import ProtectClient
from .saved import SavedStore
from .signing import SignedUrls


@dataclass(slots=True)
class RuntimeData:
    """Everything the HTTP views and pre-warmer need at runtime."""

    client: ProtectClient
    thumbs: ThumbnailCache
    clips: ClipCache
    # Signs the thumbnail/clip URLs the lists hand out (signing.py).
    media: SignedUrls
    ws_unsub: object | None = None
    # Clips kept beyond the NVR's retention (saved.py).
    saved: SavedStore | None = None
