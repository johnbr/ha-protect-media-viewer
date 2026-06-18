"""Unit tests for pure helper functions across the integration.

These exercise small, pure pieces of logic — HMAC media-token signing/verifying,
media-URL construction, ISO parsing, query-param coercion, JPEG sniffing,
size-capped LRU pruning, and the card cache-bust fingerprint — without standing
up Home Assistant. See ``conftest.py`` for the import-time stubbing strategy.
"""

from __future__ import annotations

import json
import os
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

from custom_components.protect_media_viewer import const
from custom_components.protect_media_viewer.api import (
    _check_token,
    _int_param,
    _make_token,
    _media_url,
    _parse_iso,
)
from custom_components.protect_media_viewer.cache import (
    _is_valid_jpeg,
    _prune_dir,
    _read_if_exists,
    _write_atomic,
)
from custom_components.protect_media_viewer.frontend import _card_fingerprint

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPONENT = _REPO_ROOT / "custom_components" / "protect_media_viewer"


# ---------------------------------------------------------------------------
# HMAC media tokens (api._make_token / _check_token)
# ---------------------------------------------------------------------------


def test_token_roundtrip_verifies():
    token = _make_token("s3cret", "thumb", "evt-1")
    assert _check_token("s3cret", "thumb", "evt-1", token) is True


def test_token_is_deterministic():
    assert _make_token("s3cret", "clip", "evt-9") == _make_token("s3cret", "clip", "evt-9")


def test_token_rejects_wrong_kind():
    token = _make_token("s3cret", "thumb", "evt-1")
    assert _check_token("s3cret", "clip", "evt-1", token) is False


def test_token_rejects_wrong_event():
    token = _make_token("s3cret", "thumb", "evt-1")
    assert _check_token("s3cret", "thumb", "evt-2", token) is False


def test_token_rejects_wrong_secret():
    token = _make_token("s3cret", "thumb", "evt-1")
    assert _check_token("other", "thumb", "evt-1", token) is False


def test_token_rejects_empty_or_none():
    assert _check_token("s3cret", "thumb", "evt-1", None) is False
    assert _check_token("s3cret", "thumb", "evt-1", "") is False


# ---------------------------------------------------------------------------
# Media URL construction (api._media_url)
# ---------------------------------------------------------------------------


def test_media_url_includes_token_and_entry():
    url = _media_url("thumb", "evt-1", "s3cret", "entryA")
    token = _make_token("s3cret", "thumb", "evt-1")
    assert url.startswith("/api/protect_media_viewer/thumb/evt-1?")
    assert f"t={token}" in url
    assert "&entry=entryA" in url


def test_media_url_omits_entry_when_absent():
    url = _media_url("clip", "evt-2", "s3cret", None)
    assert url.startswith("/api/protect_media_viewer/clip/evt-2?t=")
    assert "entry=" not in url


# ---------------------------------------------------------------------------
# ISO parsing (api._parse_iso)
# ---------------------------------------------------------------------------


def test_parse_iso_handles_offset_suffix():
    dt = _parse_iso("2024-04-01T18:30:00+00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_assumes_utc_when_naive():
    dt = _parse_iso("2024-04-01T18:30:00")
    assert dt is not None
    assert dt.tzinfo == UTC


def test_parse_iso_returns_none_for_empty():
    assert _parse_iso(None) is None
    assert _parse_iso("") is None


def test_parse_iso_returns_none_for_garbage():
    assert _parse_iso("not-a-date") is None


# ---------------------------------------------------------------------------
# Query-param coercion (api._int_param)
# ---------------------------------------------------------------------------


def _req(**query):
    return SimpleNamespace(query=dict(query))


def test_int_param_default_when_missing():
    assert _int_param(_req(), "limit", 60, 200) == 60


def test_int_param_clamps_to_maximum():
    assert _int_param(_req(limit="999"), "limit", 60, 200) == 200


def test_int_param_floors_at_zero():
    assert _int_param(_req(limit="-5"), "limit", 60, 200) == 0


def test_int_param_default_on_garbage():
    assert _int_param(_req(limit="abc"), "limit", 60, 200) == 60


def test_int_param_passes_valid_value():
    assert _int_param(_req(limit="42"), "limit", 60, 200) == 42


# ---------------------------------------------------------------------------
# JPEG sniffing (cache._is_valid_jpeg)
# ---------------------------------------------------------------------------


def test_is_valid_jpeg_accepts_soi_and_size():
    assert _is_valid_jpeg(b"\xff\xd8" + b"\x00" * 1000) is True


def test_is_valid_jpeg_rejects_tiny():
    assert _is_valid_jpeg(b"\xff\xd8" + b"\x00" * 10) is False


def test_is_valid_jpeg_rejects_wrong_magic():
    assert _is_valid_jpeg(b"PNG\r\n" + b"\x00" * 1000) is False


# ---------------------------------------------------------------------------
# Atomic write / read (cache._write_atomic / _read_if_exists)
# ---------------------------------------------------------------------------


def test_write_atomic_roundtrip(tmp_path):
    target = tmp_path / "nested" / "file.bin"
    _write_atomic(target, b"hello")
    assert _read_if_exists(target) == b"hello"
    # No stray temp file left behind.
    assert not (target.parent / "file.tmp").exists()


def test_read_if_exists_missing_returns_none(tmp_path):
    assert _read_if_exists(tmp_path / "nope.bin") is None


# ---------------------------------------------------------------------------
# Size-capped LRU pruning (cache._prune_dir)
# ---------------------------------------------------------------------------


def test_prune_dir_evicts_oldest_until_under_cap(tmp_path):
    # Three 1000-byte files; cap at 1500 -> oldest two must be evicted.
    names = ["old", "mid", "new"]
    for i, name in enumerate(names):
        p = tmp_path / f"{name}.bin"
        p.write_bytes(b"\x00" * 1000)
        # Strictly increasing atime so eviction order is deterministic.
        os.utime(p, (1_000_000 + i, 1_000_000 + i))

    _prune_dir(tmp_path, max_bytes=1500)

    assert not (tmp_path / "old.bin").exists()
    assert not (tmp_path / "mid.bin").exists()
    assert (tmp_path / "new.bin").exists()


def test_prune_dir_noop_when_under_cap(tmp_path):
    p = tmp_path / "small.bin"
    p.write_bytes(b"\x00" * 100)
    _prune_dir(tmp_path, max_bytes=10_000)
    assert p.exists()


# ---------------------------------------------------------------------------
# Card cache-bust fingerprint (frontend._card_fingerprint)
# ---------------------------------------------------------------------------


def test_card_fingerprint_is_short_hex():
    fp = _card_fingerprint(_COMPONENT / "frontend" / "protect-media-viewer-card.js")
    assert len(fp) == 12
    assert all(c in "0123456789abcdef" for c in fp)


def test_card_fingerprint_changes_with_content(tmp_path):
    a = tmp_path / "a.js"
    b = tmp_path / "b.js"
    a.write_bytes(b"console.log(1)")
    b.write_bytes(b"console.log(2)")
    assert _card_fingerprint(a) != _card_fingerprint(b)
    # Same content -> same fingerprint.
    assert _card_fingerprint(a) == _card_fingerprint(a)


def test_card_fingerprint_missing_file_is_empty(tmp_path):
    assert _card_fingerprint(tmp_path / "absent.js") == ""


# ---------------------------------------------------------------------------
# Version sync — the exact failure class that motivated the cache-bust fix.
# ---------------------------------------------------------------------------


def test_const_version_matches_manifest():
    manifest = json.loads((_COMPONENT / "manifest.json").read_text())
    assert manifest["version"] == const.VERSION
