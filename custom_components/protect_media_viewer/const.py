"""Constants for the Protect Media Viewer integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "protect_media_viewer"
# Bumped automatically by release-please (kept in lockstep with manifest.json).
# Prefixes the frontend card cache-bust token; the authoritative buster is the
# card file's content hash (see frontend.py).
VERSION: Final = "0.4.1"  # x-release-please-version

# Config entry keys
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_VERIFY_SSL: Final = "verify_ssl"
# Retired: the secret behind the old never-expiring media tokens. Kept so setup
# can remove it from entries created before signed paths.
CONF_URL_SECRET: Final = "url_secret"

DEFAULT_PORT: Final = 443
DEFAULT_VERIFY_SSL: Final = False

# Options
CONF_MIN_CONFIDENCE: Final = "min_confidence"
CONF_CLIP_CACHE_MB: Final = "clip_cache_mb"
DEFAULT_MIN_CONFIDENCE: Final = 50
DEFAULT_CLIP_CACHE_MB: Final = 5120

# Saved clips (options flow). Saved clips are copies kept outside Protect so
# they outlive the NVR's retention; see saved.py.
CONF_SAVED_DIR: Final = "saved_dir"
CONF_SAVE_PRE_ROLL: Final = "save_pre_roll"
CONF_SAVE_POST_ROLL: Final = "save_post_roll"
CONF_SAVE_MIN_FREE_GB: Final = "save_min_free_gb"
CONF_TRASH_DAYS: Final = "trash_days"
CONF_FLAG_IN_PROTECT: Final = "flag_in_protect"
CONF_REMOVE_ADMIN_ONLY: Final = "remove_admin_only"
# Empty = <config>/protect_media_viewer/saved.
DEFAULT_SAVED_DIR: Final = ""
DEFAULT_SAVE_PRE_ROLL: Final = 10
DEFAULT_SAVE_POST_ROLL: Final = 10
DEFAULT_SAVE_MIN_FREE_GB: Final = 10
DEFAULT_TRASH_DAYS: Final = 30
DEFAULT_FLAG_IN_PROTECT: Final = False
DEFAULT_REMOVE_ADMIN_ONLY: Final = True
# A configured saved_dir is only written to while this file exists in it, so
# an unmounted disk can't silently turn into a directory on the root fs.
SAVED_MARKER: Final = ".protect_media_viewer_saved"

# Smart-detection types we expose as filters in the UI.
SMART_DETECT_TYPES: Final = ["person", "vehicle", "animal", "face", "licensePlate"]
