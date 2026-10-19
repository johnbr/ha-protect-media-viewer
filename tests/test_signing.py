"""Signed media URLs: what is signed, and when a signed URL is reused."""

from __future__ import annotations

from datetime import timedelta

from custom_components.protect_media_viewer.signing import MEDIA_URL_EXPIRY, SignedUrls, media_path


class _Signer:
    """Stands in for async_sign_path: records calls, numbers each signature."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, timedelta, str | None]] = []

    def __call__(self, path: str, expiration: timedelta, refresh_token_id: str | None) -> str:
        self.calls.append((path, expiration, refresh_token_id))
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}authSig=sig{len(self.calls)}"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _urls(**kw):
    signer, clock = _Signer(), _Clock()
    return SignedUrls(signer, clock=clock, **kw), signer, clock


# --- the path that gets signed ----------------------------------------------


def test_every_parameter_is_on_the_path_before_signing() -> None:
    """HA signs the query too, so nothing may be appended after signing."""
    assert media_path("saved-clip", "e1", "entryA", download=True) == (
        "/api/protect_media_viewer/saved-clip/e1?entry=entryA&download=1"
    )


def test_no_query_without_parameters() -> None:
    assert media_path("thumb", "e1", None) == "/api/protect_media_viewer/thumb/e1"


def test_urls_expire_after_a_day_and_name_the_user() -> None:
    urls, signer, _ = _urls()
    urls.url("/p", "token-A")
    assert signer.calls == [("/p", MEDIA_URL_EXPIRY, "token-A")]
    assert timedelta(hours=1) <= MEDIA_URL_EXPIRY <= timedelta(days=7)


# --- reuse --------------------------------------------------------------------


def test_a_fresh_url_is_reused_so_the_browser_cache_hits() -> None:
    urls, signer, clock = _urls()
    first = urls.url("/p", "token-A")
    clock.now += MEDIA_URL_EXPIRY.total_seconds() / 2 - 1
    assert urls.url("/p", "token-A") == first
    assert len(signer.calls) == 1


def test_a_url_past_half_its_life_is_signed_again() -> None:
    """So every URL handed out has at least half its lifetime left."""
    urls, signer, clock = _urls()
    first = urls.url("/p", "token-A")
    clock.now += MEDIA_URL_EXPIRY.total_seconds() / 2
    assert urls.url("/p", "token-A") != first
    assert len(signer.calls) == 2


def test_urls_are_never_shared_between_logins() -> None:
    urls, signer, _ = _urls()
    a = urls.url("/p", "token-A")
    b = urls.url("/p", "token-B")
    assert a != b
    assert [call[2] for call in signer.calls] == ["token-A", "token-B"]


def test_the_cache_is_bounded() -> None:
    urls, signer, _ = _urls(max_cached=2)
    urls.url("/a", "t")
    urls.url("/b", "t")
    urls.url("/c", "t")  # evicts /a, the least recently used
    urls.url("/a", "t")
    assert len(signer.calls) == 4


def test_clear_forgets_everything() -> None:
    urls, signer, _ = _urls()
    urls.url("/p", "t")
    urls.clear()
    urls.url("/p", "t")
    assert len(signer.calls) == 2


# --- per event ------------------------------------------------------------------


def test_a_live_event_without_a_saved_copy_has_no_download() -> None:
    urls, _, _ = _urls()
    media = urls.event_media("e1", "entryA", "t", saved=False, has_saved_copy=False)
    assert set(media) == {"thumbnail", "clip"}
    assert media["thumbnail"].startswith("/api/protect_media_viewer/thumb/e1?entry=entryA&authSig=")
    assert media["clip"].startswith("/api/protect_media_viewer/clip/e1?entry=entryA&authSig=")


def test_a_live_event_with_a_saved_copy_downloads_the_copy() -> None:
    urls, _, _ = _urls()
    media = urls.event_media("e1", None, "t", saved=False, has_saved_copy=True)
    assert media["download"].startswith("/api/protect_media_viewer/saved-clip/e1?download=1&authSig=")


def test_a_saved_clip_is_served_from_the_saved_directory() -> None:
    urls, _, _ = _urls()
    media = urls.event_media("e1", None, "t", saved=True, has_saved_copy=True)
    assert media["thumbnail"].startswith("/api/protect_media_viewer/saved-thumb/e1?authSig=")
    assert media["clip"].startswith("/api/protect_media_viewer/saved-clip/e1?authSig=")
    assert media["download"].startswith("/api/protect_media_viewer/saved-clip/e1?download=1&authSig=")
