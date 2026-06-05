"""Constants for the Protect Media Viewer integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "protect_media_viewer"
# Keep in sync with manifest.json; used to cache-bust the frontend card.
VERSION: Final = "0.1.11"

# Config entry keys
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_VERIFY_SSL: Final = "verify_ssl"
# Persisted secret used to sign stable media (thumbnail/clip) tokens.
CONF_URL_SECRET: Final = "url_secret"

DEFAULT_PORT: Final = 443
DEFAULT_VERIFY_SSL: Final = False

# Options
CONF_MIN_CONFIDENCE: Final = "min_confidence"
CONF_CLIP_CACHE_MB: Final = "clip_cache_mb"
DEFAULT_MIN_CONFIDENCE: Final = 50
DEFAULT_CLIP_CACHE_MB: Final = 5120

# Smart-detection types we expose as filters in the UI.
SMART_DETECT_TYPES: Final = ["person", "vehicle", "animal", "face", "licensePlate"]
