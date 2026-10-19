"""Unit tests for the saved-clips store (saved.py) and its API helpers.

No NVR and no Home Assistant: a fake hass runs executor jobs inline, and a fake
client stands in for ProtectClient (event lookup, clip export, thumbnail, flag).
Everything happens in a pytest tmp dir.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.protect_media_viewer import api
from custom_components.protect_media_viewer.const import SAVED_MARKER
from custom_components.protect_media_viewer.saved import (
    TRASH_DIR,
    SavedConfig,
    SavedStore,
    SaveError,
    base_name,
    clean_note,
    clip_window,
    prepare_saved_dir,
    slug,
    valid_event_id,
)
from custom_components.protect_media_viewer.signing import SignedUrls

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
JPEG = b"\xff\xd8" + b"\x00" * 2000
EV_ID = "6720a1b2c3d4e5f600112233"


def _event(event_id=EV_ID, camera="cam1", name="Driveway", start=None, end=None, types=("person",)):
    start = start or NOW - timedelta(days=3)
    return {
        "id": event_id,
        "camera_id": camera,
        "camera_name": name,
        "start": start.isoformat(),
        "end": (end or start + timedelta(seconds=20)).isoformat(),
        "score": 88,
        "smart_detect_types": list(types),
        "flagged": False,
    }


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeClient:
    def __init__(self, events=None, export_ok=True, thumb=JPEG, flag_ok=True):
        self.events = {e["id"]: e for e in (events if events is not None else [_event()])}
        self.export_ok = export_ok
        self.thumb = thumb
        self.flag_ok = flag_ok
        self.exports = []
        self.flags = []

    async def get_event_record(self, event_id):
        ev = self.events.get(event_id)
        return dict(ev) if ev else None

    async def export_clip_to(self, camera_id, start, end, dest, channel_index=0):
        self.exports.append((camera_id, start, end, Path(dest).name))
        if not self.export_ok:
            Path(dest).write_bytes(b"")
            return False
        Path(dest).write_bytes(b"MP4" * 1000)
        return True

    async def get_thumbnail(self, event_id, width=None):
        return self.thumb

    async def set_flagged(self, event_id, flagged):
        self.flags.append((event_id, flagged))
        return self.flag_ok


def _config(root: Path, **kw) -> SavedConfig:
    base = {"root": root, "create_marker": False, "marker": SAVED_MARKER, "min_free_bytes": 0}
    base.update(kw)
    return SavedConfig(**base)


def _store(root: Path, client=None, clock=lambda: NOW, **kw) -> SavedStore:
    return SavedStore(FakeHass(), client or FakeClient(), _config(root, **kw), tz=UTC, clock=clock)


def _marked(tmp_path: Path) -> Path:
    root = tmp_path / "saved"
    root.mkdir()
    (root / SAVED_MARKER).touch()
    return root


def run(coro):
    return asyncio.run(coro)


async def _init(store: SavedStore) -> SavedStore:
    await store.async_init()
    return store


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("good", [EV_ID, "abc", "a_b-C9", "x" * 64])
def test_valid_event_id_accepts(good):
    assert valid_event_id(good)


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "a.b", "x" * 65, None, 12, "a b", "é"])
def test_valid_event_id_rejects(bad):
    assert not valid_event_id(bad)


def test_slug_and_base_name():
    assert slug("Front Door #2") == "front_door_2"
    assert slug("!!!") == "camera"
    ev = _event(start=datetime(2026, 10, 14, 5, 15, 4, tzinfo=UTC))
    # Named in LOCAL time so the files sort and read the way people think.
    pacific = timezone(timedelta(hours=-7))
    assert base_name(ev, pacific) == f"2026-10-13_221504_driveway_{EV_ID}"


def test_clean_note_is_single_line_and_capped():
    assert clean_note("  a\nb\tc  ") == "a b c"
    assert clean_note(None) == ""
    assert len(clean_note("x" * 2000)) == 500


def test_clip_window_pads_whole_event():
    start = NOW - timedelta(minutes=5)
    ev = _event(start=start, end=start + timedelta(seconds=95))
    s, e = clip_window(ev, 10, 10, NOW)
    assert s == start - timedelta(seconds=10)
    assert e == start + timedelta(seconds=105)


def test_clip_window_open_event_never_runs_past_now():
    start = NOW - timedelta(seconds=2)
    ev = _event(start=start)
    ev["end"] = None
    s, e = clip_window(ev, 10, 10, NOW)
    assert s == start - timedelta(seconds=10)
    assert e == NOW


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def test_save_writes_clip_thumb_and_sidecar(tmp_path):
    root = _marked(tmp_path)
    client = FakeClient()
    store = run(_init(_store(root, client)))

    rec, created = run(store.async_save(EV_ID, "  package\nthief ", "John"))

    assert created is True
    base = f"2026/10/2026-10-11_120000_driveway_{EV_ID}"
    assert rec["_base"] == base
    assert (root / f"{base}.mp4").read_bytes().startswith(b"MP4")
    assert (root / f"{base}.jpg").read_bytes() == JPEG
    sidecar = json.loads((root / f"{base}.json").read_text())
    assert sidecar["event"]["id"] == EV_ID
    assert sidecar["saved"]["note"] == "package thief"
    assert sidecar["saved"]["saved_by"] == "John"
    assert sidecar["saved"]["has_thumbnail"] is True
    assert sidecar["saved"]["protect_flagged"] is False
    assert "_base" not in sidecar
    # Whole event (20 s) plus 10 s either side.
    _, start, end, _ = client.exports[0]
    assert end - start == timedelta(seconds=40)
    assert not list(root.rglob("*.part")) and not list(root.rglob("*.tmp"))
    assert store.saved_info(EV_ID)["note"] == "package thief"
    assert client.flags == []


def test_save_is_idempotent(tmp_path):
    root = _marked(tmp_path)
    client = FakeClient()
    store = run(_init(_store(root, client)))
    run(store.async_save(EV_ID))
    rec, created = run(store.async_save(EV_ID, "second"))
    assert created is False
    assert len(client.exports) == 1
    assert rec["saved"]["note"] == ""


def test_concurrent_saves_export_once(tmp_path):
    root = _marked(tmp_path)
    client = FakeClient()
    store = run(_init(_store(root, client)))

    async def both():
        return await asyncio.gather(store.async_save(EV_ID), store.async_save(EV_ID))

    results = run(both())
    assert sorted(created for _, created in results) == [False, True]
    assert len(client.exports) == 1


def test_save_refused_without_marker(tmp_path):
    root = tmp_path / "unmounted"
    root.mkdir()
    client = FakeClient()
    store = run(_init(_store(root, client)))
    with pytest.raises(SaveError) as err:
        run(store.async_save(EV_ID))
    assert err.value.code == "unavailable"
    assert client.exports == []
    assert list(root.iterdir()) == []
    status = run(store.async_status())
    assert status["available"] is False and "marker" in status["reason"]


def test_save_refused_when_dir_missing(tmp_path):
    store = run(_init(_store(tmp_path / "nope")))
    with pytest.raises(SaveError) as err:
        run(store.async_save(EV_ID))
    assert err.value.code == "unavailable"
    assert not (tmp_path / "nope").exists()


def test_default_dir_creates_itself_and_marker(tmp_path):
    root = tmp_path / "config" / "protect_media_viewer" / "saved"
    store = run(_init(_store(root, create_marker=True)))
    assert (root / SAVED_MARKER).is_file()
    run(store.async_save(EV_ID))
    assert store.get(EV_ID) is not None


def test_save_refused_below_free_space_floor(tmp_path):
    root = _marked(tmp_path)
    client = FakeClient()
    store = run(_init(_store(root, client, min_free_bytes=10**18)))
    with pytest.raises(SaveError) as err:
        run(store.async_save(EV_ID))
    assert err.value.code == "low_space"
    assert client.exports == []


def test_save_event_gone_from_nvr(tmp_path):
    store = run(_init(_store(_marked(tmp_path), FakeClient(events=[]))))
    with pytest.raises(SaveError) as err:
        run(store.async_save(EV_ID))
    assert err.value.code == "not_found"


def test_failed_export_leaves_nothing_behind(tmp_path):
    root = _marked(tmp_path)
    store = run(_init(_store(root, FakeClient(export_ok=False))))
    with pytest.raises(SaveError) as err:
        run(store.async_save(EV_ID))
    assert err.value.code == "export_failed"
    assert [p for p in root.rglob("*") if p.is_file() and p.name != SAVED_MARKER] == []
    assert store.get(EV_ID) is None


def test_invalid_id_never_touches_disk(tmp_path):
    root = _marked(tmp_path)
    client = FakeClient()
    store = run(_init(_store(root, client)))
    for bad in ("../../etc/passwd", "a/b"):
        with pytest.raises(SaveError) as err:
            run(store.async_save(bad))
        assert err.value.code == "invalid_id"
    assert client.exports == []


def test_thumbnail_falls_back_to_cached_copy(tmp_path):
    root = _marked(tmp_path)
    cached = b"\xff\xd8" + b"\x01" * 1500

    async def fallback(event_id):
        return cached

    store = SavedStore(
        FakeHass(), FakeClient(thumb=None), _config(root), tz=UTC, clock=lambda: NOW, fallback_thumbnail=fallback
    )
    run(store.async_init())
    rec, _ = run(store.async_save(EV_ID))
    assert (root / f"{rec['_base']}.jpg").read_bytes() == cached


def test_no_thumbnail_at_all_still_saves(tmp_path):
    root = _marked(tmp_path)
    store = run(_init(_store(root, FakeClient(thumb=b"not a jpeg"))))
    rec, _ = run(store.async_save(EV_ID))
    assert rec["saved"]["has_thumbnail"] is False
    assert not (root / f"{rec['_base']}.jpg").exists()


def test_flag_in_protect_mirrors_save_and_remove(tmp_path):
    root = _marked(tmp_path)
    client = FakeClient()
    store = run(_init(_store(root, client, flag_in_protect=True)))
    rec, _ = run(store.async_save(EV_ID))
    assert rec["saved"]["protect_flagged"] is True
    assert rec["event"]["flagged"] is True
    run(store.async_remove(EV_ID))
    assert client.flags == [(EV_ID, True), (EV_ID, False)]


def test_preexisting_protect_flag_is_left_alone(tmp_path):
    root = _marked(tmp_path)
    ev = _event()
    ev["flagged"] = True
    client = FakeClient(events=[ev])
    store = run(_init(_store(root, client, flag_in_protect=True)))
    rec, _ = run(store.async_save(EV_ID))
    assert rec["event"]["flagged"] is True
    assert rec["saved"]["protect_flagged"] is False
    run(store.async_remove(EV_ID))
    assert client.flags == []


def test_failed_flag_does_not_fail_the_save(tmp_path):
    root = _marked(tmp_path)
    client = FakeClient(flag_ok=False)
    store = run(_init(_store(root, client, flag_in_protect=True)))
    rec, created = run(store.async_save(EV_ID))
    assert created and rec["saved"]["protect_flagged"] is False
    run(store.async_remove(EV_ID))
    # Never flagged, so nothing to undo.
    assert client.flags == [(EV_ID, True)]


# ---------------------------------------------------------------------------
# Index, notes, remove, trash
# ---------------------------------------------------------------------------


def test_index_rebuilds_from_sidecars(tmp_path):
    root = _marked(tmp_path)
    store = run(_init(_store(root)))
    run(store.async_save(EV_ID, "keep"))

    # Junk the scan must survive: unreadable JSON, a sidecar without its clip.
    (root / "2026" / "10" / "broken.json").write_text("{nope")
    orphan = {"event": {"id": "orphan1"}, "saved": {}}
    (root / "2026" / "10" / "orphan.json").write_text(json.dumps(orphan))
    (root / "2026" / "10" / "half.mp4.part").write_bytes(b"x")

    fresh = run(_init(_store(root)))
    assert fresh.get(EV_ID)["saved"]["note"] == "keep"
    assert fresh.get("orphan1") is None
    # Startup cleaned the interrupted export.
    assert not (root / "2026" / "10" / "half.mp4.part").exists()


def test_update_note(tmp_path):
    root = _marked(tmp_path)
    store = run(_init(_store(root)))
    rec, _ = run(store.async_save(EV_ID))
    run(store.async_update_note(EV_ID, "new\nnote"))
    assert json.loads((root / f"{rec['_base']}.json").read_text())["saved"]["note"] == "new note"
    with pytest.raises(SaveError) as err:
        run(store.async_update_note("someotherid", "x"))
    assert err.value.code == "not_saved"


def test_remove_moves_to_trash_and_purge_honours_age(tmp_path):
    root = _marked(tmp_path)
    clock = {"now": NOW}
    store = run(_init(_store(root, clock=lambda: clock["now"], trash_days=30)))
    rec, _ = run(store.async_save(EV_ID))
    name = Path(rec["_base"]).name

    run(store.async_remove(EV_ID, "John"))
    assert store.get(EV_ID) is None
    assert not (root / f"{rec['_base']}.mp4").exists()
    trash = root / TRASH_DIR
    assert {p.name for p in trash.iterdir()} == {f"{name}.mp4", f"{name}.jpg", f"{name}.json"}
    removed = json.loads((trash / f"{name}.json").read_text())["saved"]
    assert removed["removed_by"] == "John"

    # A rescan must not resurrect it from the trash.
    run(store.async_maintain())
    assert store.get(EV_ID) is None

    clock["now"] = NOW + timedelta(days=29)
    run(store.async_maintain())
    assert (trash / f"{name}.mp4").exists()

    clock["now"] = NOW + timedelta(days=31)
    run(store.async_maintain())
    assert list(trash.iterdir()) == []


def test_resave_after_remove(tmp_path):
    root = _marked(tmp_path)
    client = FakeClient()
    store = run(_init(_store(root, client)))
    run(store.async_save(EV_ID))
    run(store.async_remove(EV_ID))
    _, created = run(store.async_save(EV_ID))
    assert created is True
    assert len(client.exports) == 2


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _populated(tmp_path):
    events = [
        _event("e1", "cam1", "Driveway", start=NOW - timedelta(days=1), types=("person",)),
        _event("e2", "cam2", "Garage", start=NOW - timedelta(days=40), types=("vehicle",)),
        _event("e3", "cam1", "Driveway", start=NOW - timedelta(days=400), types=("animal", "person")),
    ]
    store = run(_init(_store(_marked(tmp_path), FakeClient(events=events))))
    run(store.async_save("e1", "Package thief", "John"))
    run(store.async_save("e2", "plumber van", "Lisa"))
    run(store.async_save("e3", "", "John"))
    return store


def test_list_orders_newest_first_and_pages(tmp_path):
    store = _populated(tmp_path)
    recs, total = store.list()
    assert [r["event"]["id"] for r in recs] == ["e1", "e2", "e3"] and total == 3
    page, total = store.list(offset=1, limit=1)
    assert [r["event"]["id"] for r in page] == ["e2"] and total == 3


def test_list_filters(tmp_path):
    store = _populated(tmp_path)

    def ids(**kw):
        return [r["event"]["id"] for r in store.list(**kw)[0]]

    assert ids(camera="cam1") == ["e1", "e3"]
    assert ids(types=["person"]) == ["e1", "e3"]
    assert ids(types=["vehicle", "animal"]) == ["e2", "e3"]
    assert ids(query="THIEF") == ["e1"]
    assert ids(query="lisa") == ["e2"]
    assert ids(query="garage") == ["e2"]
    assert ids(since=NOW - timedelta(days=30)) == ["e1"]


# ---------------------------------------------------------------------------
# Options-flow directory check
# ---------------------------------------------------------------------------


def test_prepare_saved_dir(tmp_path):
    assert prepare_saved_dir("relative/path", SAVED_MARKER) == "dir_not_absolute"
    assert prepare_saved_dir(str(tmp_path / "missing"), SAVED_MARKER) == "dir_missing"
    assert not (tmp_path / "missing").exists()  # never created
    assert prepare_saved_dir(str(tmp_path), SAVED_MARKER) is None
    assert (tmp_path / SAVED_MARKER).is_file()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def test_saved_event_shape_and_signed_media(tmp_path):
    root = _marked(tmp_path)
    store = run(_init(_store(root)))
    rec, _ = run(store.async_save(EV_ID, "n", "John"))
    signed = []

    def sign(path, expiration, refresh_token_id):
        signed.append(path)
        return f"{path}{'&' if '?' in path else '?'}authSig=x"

    runtime = SimpleNamespace(media=SignedUrls(sign), saved=store)
    ev = api._saved_event(rec, runtime, {}, None)
    assert ev["id"] == EV_ID and ev["camera_name"] == "Driveway"
    assert ev["saved"]["note"] == "n" and "_base" not in ev["saved"]
    assert "/saved-clip/" in ev["clip"] and "/saved-thumb/" in ev["thumbnail"]
    # download=1 is part of what was signed, not tacked on after it.
    assert f"/api/protect_media_viewer/saved-clip/{EV_ID}?download=1" in signed
    assert ev["download"].endswith("download=1&authSig=x")


def test_can_remove_respects_admin_option(tmp_path):
    root = _marked(tmp_path)
    admin = {"hass_user": SimpleNamespace(is_admin=True, name="John")}
    user = {"hass_user": SimpleNamespace(is_admin=False, name="Lisa")}

    strict = SimpleNamespace(saved=_store(root, remove_admin_only=True))
    assert api._can_remove(strict, admin) is True
    assert api._can_remove(strict, user) is False
    assert api._can_remove(strict, {}) is False

    relaxed = SimpleNamespace(saved=_store(root, remove_admin_only=False))
    assert api._can_remove(relaxed, user) is True
    assert api._can_remove(SimpleNamespace(saved=None), admin) is False
