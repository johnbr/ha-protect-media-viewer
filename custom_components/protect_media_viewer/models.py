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
    ws_unsub: object | None = None
    # Cache of stable signed URLs keyed by path: {path: (signed_url, exp_ts)}.
    # Reusing them keeps thumbnail URLs identical across /events calls so the
    # browser can cache the images instead of re-downloading on every scroll.
    signed_urls: dict[str, tuple[str, int]] = field(default_factory=dict)
