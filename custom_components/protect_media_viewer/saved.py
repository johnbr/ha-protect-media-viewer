"""Saved clips: copies of Protect events kept beyond the NVR's retention.

Protect deletes footage (and the event rows with it) once it ages out of the
console's retention window. Its own "flag" (``isFavorite``) does NOT stop that:
Ubiquiti documents flagging only as protection against Enhanced Retention's
resolution downgrade. So a clip worth keeping is exported here: a full-res MP4
of the event window plus padding, a thumbnail, and a JSON sidecar.

Layout under the saved directory::

    .protect_media_viewer_saved          marker (see "Mount guard")
    2026/10/2026-10-13_221504_driveway_<event_id>.mp4
    2026/10/2026-10-13_221504_driveway_<event_id>.jpg
    2026/10/2026-10-13_221504_driveway_<event_id>.json   written LAST
    .trash/                              removed clips, purged after N days

The sidecars ARE the record. There is no database or index file: the store
scans ``*.json`` into memory at startup (and on each maintenance pass), so the
folder stays meaningful with Home Assistant gone, and a clip copied in or
deleted by hand is picked up on the next scan. A sidecar is written only after
its MP4 is complete, so a crash mid-save leaves a ``.part`` file (cleaned up at
startup) rather than a record pointing at a broken clip.

Mount guard: a configured directory is usually a mount (a NAS share, a second
disk bind-mounted into a container). If that mount is missing, Docker happily
creates an empty directory in its place on the root filesystem and saves would
land there silently. So the store refuses to write unless the marker file is
present. The options flow drops the marker when a directory is configured; the
default directory (inside the HA config dir) creates its own.

Pure apart from ``hass.async_add_executor_job`` and the client calls, so the
unit tests drive it with a fake hass and a fake client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from .cache import _is_valid_jpeg, _unlink_quiet

_LOGGER = logging.getLogger(__name__)

SIDECAR_VERSION = 1
TRASH_DIR = ".trash"

# Protect event ids are 24-hex ObjectIds today; accept any short id made of
# filename-safe characters, and nothing else. This runs before ANY filesystem
# use on the authenticated write paths, so ".." or "/" can never reach a path.
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Length used when an event has no end yet (still in progress).
_FALLBACK_LEN = timedelta(seconds=6)
# High-resolution channel.
_CHANNEL_INDEX = 0
_NOTE_MAX = 500
# Exports are I/O heavy on the NVR; don't let a burst of saves stack up.
_MAX_CONCURRENT_EXPORTS = 2
_MEDIA_EXTS = (".mp4", ".jpg")


class SaveError(Exception):
    """A save/remove/edit the store refused, with a code the card can show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True, frozen=True)
class SavedConfig:
    """Options that shape the store (all from the options flow)."""

    root: Path
    # True only for the integration's own default directory: it may create
    # itself and its marker. A configured directory must already carry one.
    create_marker: bool
    marker: str
    pre_roll_s: int = 10
    post_roll_s: int = 10
    min_free_bytes: int = 10 * 1024**3
    trash_days: int = 30
    flag_in_protect: bool = False
    remove_admin_only: bool = True


def valid_event_id(event_id: object) -> bool:
    return isinstance(event_id, str) and bool(_EVENT_ID_RE.fullmatch(event_id))


def clean_note(note: object) -> str:
    """Printable, single-line, length-capped note text."""
    if not isinstance(note, str):
        return ""
    text = " ".join(note.split())
    return "".join(ch for ch in text if ch.isprintable())[:_NOTE_MAX]


def slug(name: str) -> str:
    """Filename-safe camera name: ``Front Door #2`` -> ``front_door_2``."""
    out = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return out[:40] or "camera"


def base_name(event: dict[str, Any], tz: tzinfo) -> str:
    """``2026-10-13_221504_driveway_<id>``: sorts by time, readable in a file manager."""
    start = _parse_iso(event.get("start")) or datetime.now(tz=UTC)
    local = start.astimezone(tz)
    return f"{local:%Y-%m-%d_%H%M%S}_{slug(event.get('camera_name') or '')}_{event['id']}"


def clip_window(
    event: dict[str, Any], pre_roll_s: int, post_roll_s: int, now: datetime
) -> tuple[datetime, datetime]:
    """The span to export: the whole event plus padding, never past ``now``."""
    start = _parse_iso(event.get("start"))
    if start is None:
        raise SaveError("not_found", "The event has no start time.")
    end = _parse_iso(event.get("end")) or start + _FALLBACK_LEN
    clip_start = start - timedelta(seconds=pre_roll_s)
    clip_end = min(end + timedelta(seconds=post_roll_s), now)
    return clip_start, max(clip_end, start + timedelta(seconds=1))


class SavedStore:
    """The saved-clips directory and its in-memory index."""

    def __init__(
        self,
        hass: Any,
        client: Any,
        config: SavedConfig,
        *,
        tz: tzinfo = UTC,
        fallback_thumbnail: Callable[[str], Any] | None = None,
        meta: dict[str, Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._hass = hass
        self._client = client
        self.config = config
        self._tz = tz
        self._fallback_thumbnail = fallback_thumbnail
        # Stamped into every sidecar (integration version, NVR id).
        self._meta = meta or {}
        self._clock = clock
        self._index: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._export_slots = asyncio.Semaphore(_MAX_CONCURRENT_EXPORTS)

    @property
    def root(self) -> Path:
        return self.config.root

    # ---- lifecycle ----------------------------------------------------------

    async def async_init(self) -> None:
        """Create the default dir, clean up, and load the index."""
        if self.config.create_marker:
            await self._run(_ensure_dir_with_marker, self.root, self.config.marker)
        reason = await self._run(_unavailable_reason, self.root, self.config.marker)
        if reason:
            # Not fatal: playback of the NVR's events works regardless, and the
            # card says why saving is off. Typical cause: a mount that isn't up.
            _LOGGER.warning("Saved clips unavailable (%s): %s", self.root, _REASONS[reason])
            return
        await self._run(_clean_parts, self.root)
        await self.async_maintain()

    async def async_maintain(self) -> None:
        """Purge old trash and re-read the sidecars (picks up hand edits)."""
        if await self._run(_unavailable_reason, self.root, self.config.marker):
            return
        purged = await self._run(_purge_trash, self.root, self.config.trash_days, self._clock())
        if purged:
            _LOGGER.info("Purged %d removed clip(s) older than %d days", purged, self.config.trash_days)
        self._index = await self._run(_scan, self.root)

    async def async_status(self) -> dict[str, Any]:
        reason = await self._run(_unavailable_reason, self.root, self.config.marker)
        return {
            "available": reason is None,
            "reason": _REASONS[reason] if reason else None,
            "count": len(self._index),
        }

    # ---- reads (in-memory) ----------------------------------------------------

    def get(self, event_id: str) -> dict[str, Any] | None:
        return self._index.get(event_id)

    def saved_info(self, event_id: str) -> dict[str, Any] | None:
        """The public ``saved`` object for an event, or None if not saved."""
        rec = self._index.get(event_id)
        return public_info(rec) if rec else None

    def media_path(self, event_id: str, ext: str) -> Path | None:
        rec = self._index.get(event_id)
        if rec is None or ext not in _MEDIA_EXTS:
            return None
        return self.root / f"{rec['_base']}{ext}"

    def download_name(self, event_id: str, ext: str) -> str:
        rec = self._index.get(event_id)
        return f"{Path(rec['_base']).name}{ext}" if rec else f"{event_id}{ext}"

    def list(
        self,
        *,
        camera: str | None = None,
        types: list[str] | None = None,
        query: str | None = None,
        since: datetime | None = None,
        offset: int = 0,
        limit: int = 60,
    ) -> tuple[list[dict[str, Any]], int]:
        """Saved records, newest event first, filtered like ``/events``."""
        needle = (query or "").strip().lower()
        wanted = set(types or [])
        hits = []
        for rec in self._index.values():
            ev = rec["event"]
            if camera and ev.get("camera_id") != camera:
                continue
            if wanted and not wanted.intersection(ev.get("smart_detect_types") or []):
                continue
            if since is not None:
                start = _parse_iso(ev.get("start"))
                if start is None or start < since:
                    continue
            if needle and needle not in _haystack(rec):
                continue
            hits.append(rec)
        hits.sort(key=lambda r: r["event"].get("start") or "", reverse=True)
        return hits[offset : offset + limit], len(hits)

    # ---- writes ---------------------------------------------------------------

    async def async_save(self, event_id: str, note: str = "", user: str | None = None) -> tuple[dict[str, Any], bool]:
        """Export an event to the saved dir. Returns (record, created).

        Idempotent: saving an already-saved event returns the existing record
        (``created`` False). Raises SaveError for anything the user can act on.
        """
        if not valid_event_id(event_id):
            raise SaveError("invalid_id", "Invalid event id.")
        if (existing := self._index.get(event_id)) is not None:
            return existing, False

        lock = self._locks.setdefault(event_id, asyncio.Lock())
        async with lock:
            if (existing := self._index.get(event_id)) is not None:
                return existing, False
            async with self._export_slots:
                record = await self._save_locked(event_id, clean_note(note), user)
        self._index[event_id] = record
        self._locks.pop(event_id, None)
        return record, True

    async def _save_locked(self, event_id: str, note: str, user: str | None) -> dict[str, Any]:
        cfg = self.config
        if reason := await self._run(_unavailable_reason, self.root, cfg.marker):
            raise SaveError("unavailable", f"Saving is unavailable: {_REASONS[reason]}.")
        free = await self._run(_free_bytes, self.root)
        if free < cfg.min_free_bytes:
            raise SaveError(
                "low_space",
                f"Not enough free space to save ({free / 1024**3:.1f} GB free, "
                f"{cfg.min_free_bytes / 1024**3:.0f} GB kept in reserve).",
            )

        try:
            event = await self._client.get_event_record(event_id)
        except Exception as err:
            _LOGGER.warning("Could not look up event %s: %s", event_id, err)
            raise SaveError("upstream", "Could not reach the NVR to look up the event.") from err
        if event is None:
            raise SaveError("not_found", "Protect no longer has this event; its footage may have been deleted.")

        now = self._clock()
        clip_start, clip_end = clip_window(event, cfg.pre_roll_s, cfg.post_roll_s, now)
        local_start = (_parse_iso(event.get("start")) or now).astimezone(self._tz)
        rel_base = f"{local_start:%Y}/{local_start:%m}/{base_name(event, self._tz)}"
        clip_path = self.root / f"{rel_base}.mp4"
        part = clip_path.with_name(clip_path.name + ".part")
        await self._run(lambda: part.parent.mkdir(parents=True, exist_ok=True))

        try:
            ok = await self._client.export_clip_to(
                event["camera_id"], clip_start, clip_end, part, channel_index=_CHANNEL_INDEX
            )
        except Exception as err:
            _LOGGER.warning("Export failed for event %s: %s", event_id, err)
            ok = False
        if not ok:
            await self._run(_unlink_quiet, part)
            raise SaveError(
                "export_failed",
                "Protect returned no footage for this event; it may already have been deleted.",
            )
        await self._run(part.replace, clip_path)

        has_thumb = await self._save_thumbnail(event_id, self.root / f"{rel_base}.jpg")

        # protect_flagged records that WE set the flag, so a remove clears only
        # our own flag, never one somebody set in Protect beforehand.
        flagged = False
        if cfg.flag_in_protect and not event.get("flagged"):
            flagged = await self._client.set_flagged(event_id, True)
        if flagged:
            event["flagged"] = True

        size = await self._run(lambda: clip_path.stat().st_size)
        record = {
            "version": SIDECAR_VERSION,
            "event": event,
            "saved": {
                "saved_at": now.isoformat(),
                "saved_by": user,
                "note": note,
                "clip_start": clip_start.isoformat(),
                "clip_end": clip_end.isoformat(),
                "pre_roll_s": cfg.pre_roll_s,
                "post_roll_s": cfg.post_roll_s,
                "size": size,
                "has_thumbnail": has_thumb,
                "protect_flagged": flagged,
                **self._meta,
            },
        }
        # The sidecar is the commit point: written last, atomically.
        await self._run(_write_json_atomic, self.root / f"{rel_base}.json", record)
        record["_base"] = rel_base
        _LOGGER.info("Saved event %s (%s, %.1f MB) to %s", event_id, event.get("camera_name"), size / 1e6, clip_path)
        return record

    async def _save_thumbnail(self, event_id: str, dest: Path) -> bool:
        data = None
        try:
            # No width: the full-size image Protect keeps for the event.
            data = await self._client.get_thumbnail(event_id, None)
        except Exception:
            _LOGGER.debug("Full-size thumbnail fetch failed for %s", event_id, exc_info=True)
        if (not data or not _is_valid_jpeg(data)) and self._fallback_thumbnail is not None:
            try:
                data = await self._fallback_thumbnail(event_id)
            except Exception:
                data = None
        if not data or not _is_valid_jpeg(data):
            return False
        await self._run(_write_bytes_atomic, dest, data)
        return True

    async def async_update_note(self, event_id: str, note: str) -> dict[str, Any]:
        if not valid_event_id(event_id):
            raise SaveError("invalid_id", "Invalid event id.")
        rec = self._index.get(event_id)
        if rec is None:
            raise SaveError("not_saved", "That clip isn't saved.")
        if reason := await self._run(_unavailable_reason, self.root, self.config.marker):
            raise SaveError("unavailable", f"Saving is unavailable: {_REASONS[reason]}.")
        updated = {**rec, "saved": {**rec["saved"], "note": clean_note(note)}}
        await self._run(_write_json_atomic, self.root / f"{rec['_base']}.json", _strip_private(updated))
        self._index[event_id] = updated
        return updated

    async def async_remove(self, event_id: str, user: str | None = None) -> None:
        """Move a saved clip to the trash (purged after ``trash_days``)."""
        if not valid_event_id(event_id):
            raise SaveError("invalid_id", "Invalid event id.")
        rec = self._index.get(event_id)
        if rec is None:
            raise SaveError("not_saved", "That clip isn't saved.")
        if reason := await self._run(_unavailable_reason, self.root, self.config.marker):
            raise SaveError("unavailable", f"Saving is unavailable: {_REASONS[reason]}.")
        removed = _strip_private(rec)
        removed["saved"] = {**removed["saved"], "removed_at": self._clock().isoformat(), "removed_by": user}
        await self._run(_move_to_trash, self.root, rec["_base"], removed)
        self._index.pop(event_id, None)
        if rec["saved"].get("protect_flagged"):
            await self._client.set_flagged(event_id, False)
        _LOGGER.info("Removed saved clip %s (moved to %s)", event_id, TRASH_DIR)

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        return await self._hass.async_add_executor_job(func, *args)


# ---- public shape ---------------------------------------------------------------


def public_info(rec: dict[str, Any]) -> dict[str, Any]:
    """The ``saved`` object the API returns (no file paths)."""
    saved = rec.get("saved", {})
    return {
        "saved_at": saved.get("saved_at"),
        "saved_by": saved.get("saved_by"),
        "note": saved.get("note") or "",
        "size": saved.get("size"),
        "clip_start": saved.get("clip_start"),
        "clip_end": saved.get("clip_end"),
        "protect_flagged": bool(saved.get("protect_flagged")),
    }


def _haystack(rec: dict[str, Any]) -> str:
    ev, saved = rec["event"], rec.get("saved", {})
    parts = [saved.get("note") or "", saved.get("saved_by") or "", ev.get("camera_name") or ""]
    parts += [str(r.get("name") or r.get("plate") or "") for r in ev.get("recognition") or []]
    return " ".join(parts).lower()


def _strip_private(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


_REASONS = {
    "missing": "the saved-clips directory does not exist (is the disk mounted?)",
    "not_dir": "the saved-clips path is not a directory",
    "no_marker": "the saved-clips directory has no marker file (is the disk mounted?)",
}


# ---- filesystem (run in the executor) -----------------------------------------------


def _unavailable_reason(root: Path, marker: str) -> str | None:
    if not root.exists():
        return "missing"
    if not root.is_dir():
        return "not_dir"
    if not (root / marker).is_file():
        return "no_marker"
    return None


def _ensure_dir_with_marker(root: Path, marker: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / marker).touch(exist_ok=True)


def prepare_saved_dir(path: str, marker: str) -> str | None:
    """Check a configured saved-clips directory and drop its marker.

    The directory must already exist: the integration never creates a
    configured one, because a missing directory usually means a disk or share
    that isn't mounted. Writing the marker now records "this is the right
    place"; if the directory later turns up without it, saving stops instead of
    writing to whatever is underneath the mount point. Returns an error key.
    """
    root = Path(path)
    if not root.is_absolute():
        return "dir_not_absolute"
    if not root.is_dir():
        return "dir_missing"
    if not os.access(root, os.W_OK | os.X_OK):
        return "dir_not_writable"
    try:
        (root / marker).touch(exist_ok=True)
    except OSError:
        return "dir_not_writable"
    return None


def _free_bytes(root: Path) -> int:
    return shutil.disk_usage(root).free


def _scan(root: Path) -> dict[str, dict[str, Any]]:
    """Read every sidecar under ``YYYY/MM/`` into an index keyed by event id."""
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.json")):
        try:
            rec = json.loads(path.read_text())
            event_id = rec["event"]["id"]
        except (OSError, ValueError, KeyError, TypeError):
            _LOGGER.warning("Skipping unreadable saved-clip sidecar %s", path)
            continue
        if not valid_event_id(event_id):
            continue
        if not path.with_suffix(".mp4").is_file():
            _LOGGER.warning("Saved-clip sidecar %s has no .mp4 beside it; skipping", path)
            continue
        rec["_base"] = str(path.relative_to(root).with_suffix(""))
        index[event_id] = rec
    return index


def _clean_parts(root: Path) -> None:
    """Remove exports a crash or restart interrupted (nothing is in flight at startup)."""
    for pattern in ("*.part", "*.tmp"):
        for path in root.glob(f"[0-9][0-9][0-9][0-9]/[0-9][0-9]/{pattern}"):
            _unlink_quiet(path)


def _move_to_trash(root: Path, rel_base: str, removed: dict[str, Any]) -> None:
    trash = root / TRASH_DIR
    trash.mkdir(exist_ok=True)
    name = Path(rel_base).name
    # Trash sidecar first, then retire the live one: the record leaves the
    # saved list in one step, and a crash can only strand media, never lose it.
    _write_json_atomic(trash / f"{name}.json", removed)
    _unlink_quiet(root / f"{rel_base}.json")
    for ext in _MEDIA_EXTS:
        src = root / f"{rel_base}{ext}"
        if src.exists():
            src.replace(trash / f"{name}{ext}")


def _purge_trash(root: Path, days: int, now: datetime) -> int:
    trash = root / TRASH_DIR
    if not trash.is_dir():
        return 0
    cutoff = now - timedelta(days=days)
    purged = 0
    for sidecar in trash.glob("*.json"):
        removed_at = None
        try:
            removed_at = _parse_iso(json.loads(sidecar.read_text())["saved"]["removed_at"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if removed_at is None:
            removed_at = datetime.fromtimestamp(sidecar.stat().st_mtime, tz=UTC)
        if removed_at > cutoff:
            continue
        for ext in _MEDIA_EXTS:
            _unlink_quiet(sidecar.with_suffix(ext))
        _unlink_quiet(sidecar)
        purged += 1
    return purged


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    _write_bytes_atomic(path, json.dumps(data, indent=2, sort_keys=True).encode())


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
