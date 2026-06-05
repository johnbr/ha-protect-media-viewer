"""Shared runtime types (kept separate to avoid import cycles)."""

from __future__ import annotations

from dataclasses import dataclass

from .cache import ClipCache, ThumbnailCache
from .protect import ProtectClient


@dataclass(slots=True)
class RuntimeData:
    """Everything the HTTP views and pre-warmer need at runtime."""

    client: ProtectClient
    thumbs: ThumbnailCache
    clips: ClipCache
    ws_unsub: object | None = None
