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
    _CLIENT_LOG_EVENTS,
    _check_token,
    _format_client_log,
    _int_param,
    _make_token,
    _media_url,
    _parse_iso,
    _RateLimiter,
    _sanitize_client_text,
)
from custom_components.protect_media_viewer.cache import (
    _is_valid_jpeg,
    _prune_dir,
    _read_if_exists,
    _write_atomic,
)
from custom_components.protect_media_viewer.frontend import (
    _compose_loader,
    _etag_of,
    _render_loader,
)

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
# Card serving: ETag / cache-bust fingerprint + loader templating (frontend.py)
# ---------------------------------------------------------------------------


def test_etag_is_short_hex_and_deterministic():
    tag = _etag_of(b"console.log(1)")
    assert len(tag) == 32
    assert all(c in "0123456789abcdef" for c in tag)
    assert tag == _etag_of(b"console.log(1)")
    assert tag != _etag_of(b"console.log(2)")


def test_render_loader_bakes_card_url():
    template = (_COMPONENT / "frontend" / "protect-media-viewer-loader.js").read_text()
    out = _render_loader(template, "/protect_media_viewer/card.js?v=1-abc")
    assert "/protect_media_viewer/card.js?v=1-abc" in out
    assert "__CARD_URL__" not in out


def test_composed_loader_defines_the_card_before_the_loader_runs():
    """The served module must carry the card, and carry it FIRST.

    This is the whole fix for the intermittent "Configuration error": the card
    element is defined during this module's own evaluation, so it costs no
    round trip beyond the one that fetched the module. If the card ever ends
    up after the loader — or absent — the element is late again and HA paints
    its 2s-grace error card.
    """
    card = (_COMPONENT / "frontend" / "protect-media-viewer-card.js").read_text()
    loader = (_COMPONENT / "frontend" / "protect-media-viewer-loader.js").read_text()
    out = _compose_loader(card, loader, "/protect_media_viewer/card.js?v=1-abc")

    define = 'customElements.define("protect-media-viewer-card"'
    assert define in out
    assert "__protectMediaViewerLoader" in out
    assert out.index(define) < out.index("__protectMediaViewerLoader")
    # The card URL is still baked in for the fallback import path.
    assert "/protect_media_viewer/card.js?v=1-abc" in out
    assert "__CARD_URL__" not in out


def test_card_is_concatenation_safe():
    """Guard the two properties that make prepending the card legal.

    A top-level import/export would make the composed body a real ES module
    with linking semantics, and a top-level name shared with the loader would
    collide. The loader is an IIFE, so only the card's own names are exposed.
    """
    card = (_COMPONENT / "frontend" / "protect-media-viewer-card.js").read_text()
    for line in card.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import "), line
        assert not stripped.startswith("export "), line
        assert not stripped.startswith("import("), line


def test_loader_no_ops_when_the_card_is_already_defined():
    """The fallback must not fetch on the normal path."""
    loader = (_COMPONENT / "frontend" / "protect-media-viewer-loader.js").read_text()
    assert "customElements.get(TAG)" in loader
    # The short-circuit has to precede the import, or every page load pays for
    # a redundant fetch of a card it already has.
    assert loader.index("customElements.get(TAG)") < loader.index("import(url)")


def test_loader_has_retry_and_reporting_machinery():
    """Guard the loader's load-bearing pieces against accidental removal."""
    loader = (_COMPONENT / "frontend" / "protect-media-viewer-loader.js").read_text()
    # Retries must cache-bust (failed module fetches are cached in the module map).
    assert "&r=" in loader
    # Retry immediately when connectivity returns.
    assert '"online"' in loader
    # Double-delivery guard (extra_js_url + Lovelace resource).
    assert "__protectMediaViewerLoader" in loader
    # Failure reporting endpoint, matching the ClientLogView vocabulary.
    assert "/api/protect_media_viewer/log" in loader
    for event in _CLIENT_LOG_EVENTS:
        assert event in loader


# ---------------------------------------------------------------------------
# Client log endpoint helpers (api.py)
# ---------------------------------------------------------------------------


def test_sanitize_client_text_strips_and_caps():
    assert _sanitize_client_text("ok\x1b[31m\nline", 100) == "ok[31mline"
    assert _sanitize_client_text("a" * 500, 300) == "a" * 300
    assert _sanitize_client_text(None, 100) == ""
    assert _sanitize_client_text(42, 100) == ""


def test_format_client_log_composes():
    msg = _format_client_log("card_load_recovered", 4, "TypeError: x", "Mozilla/5.0")
    assert "card_load_recovered" in msg
    assert "attempts=4" in msg
    assert "Mozilla/5.0" in msg
    assert msg.endswith("TypeError: x")
    # Empty detail/UA still make a sane line.
    assert "ua=-" in _format_client_log("card_load_failed", 1, "", "")


def test_rate_limiter_sliding_window():
    clock = SimpleNamespace(now=0.0)
    limiter = _RateLimiter(3, 60, clock=lambda: clock.now)
    assert limiter.allow() and limiter.allow() and limiter.allow()
    assert not limiter.allow()  # window full
    clock.now = 61.0  # window slides
    assert limiter.allow()


# ---------------------------------------------------------------------------
# Version sync — the exact failure class that motivated the cache-bust fix.
# ---------------------------------------------------------------------------


def test_const_version_matches_manifest():
    manifest = json.loads((_COMPONENT / "manifest.json").read_text())
    assert manifest["version"] == const.VERSION
