"""Who or what Protect recognised in an event: face names and plates.

Pure (no Home Assistant / uiprotect imports) so the unit tests can feed it plain
objects. Fields are read with getattr, so a renamed upstream field degrades to
"no recognition" rather than breaking the event list.

Protect (UFP 6+) attaches a recognition GROUP to each face or plate crop in
``event.metadata.detected_thumbnails``::

    group = {id, name, matched_name, confidence}

* **known** -- the group has a ``name``: a face or plate someone labelled in
  Protect ("Johnny", "Roadrunner").
* **unknown** -- a real, clustered group nobody has named yet. Not the same as
  "stranger": an unlabelled household member lands here too.
* **unidentified** -- faces only, group id ``face_degraded_<uuid>``: the crop was
  too blurred or angled to match at all. This is the most common face result.

For a plate, ``matched_name`` is the plate TEXT and ``name`` the label; for a
face both carry the person's name once known.
"""

from __future__ import annotations

from typing import Any

KNOWN = "known"
UNKNOWN = "unknown"
UNIDENTIFIED = "unidentified"

_RANK = {UNIDENTIFIED: 0, UNKNOWN: 1, KNOWN: 2}
_DEGRADED_FACE_PREFIX = "face_degraded"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _attr(attrs: Any, key: str) -> Any:
    """One key off EventThumbnailAttributes (extra fields keep their UniFi keys)."""
    if attrs is None:
        return None
    if isinstance(attrs, dict):
        return attrs.get(key)
    value = getattr(attrs, key, None)
    if value is None:
        value = (getattr(attrs, "__pydantic_extra__", None) or {}).get(key)
    return value


def classify(thumb: Any) -> dict[str, Any] | None:
    """One detection crop -> a recognition entry, or None if it has none.

    Faces always count (a degraded crop is still worth saying "unidentified"
    about); a vehicle counts only once a plate was read, i.e. it has a group.
    """
    group = getattr(thumb, "group", None)
    thumb_type = getattr(thumb, "type", None)
    if thumb_type == "face":
        kind = "face"
    elif thumb_type == "vehicle" and group is not None:
        kind = "plate"
    else:
        return None

    group_id = _text(getattr(group, "id", None))
    name = _text(getattr(group, "name", None))
    if name:
        verdict = KNOWN
    elif kind == "face" and (not group_id or group_id.startswith(_DEGRADED_FACE_PREFIX)):
        verdict = UNIDENTIFIED
    else:
        verdict = UNKNOWN

    confidence = getattr(group, "confidence", None)
    if confidence is None:
        confidence = getattr(thumb, "confidence", None)

    entry: dict[str, Any] = {
        "kind": kind,
        "verdict": verdict,
        "name": name or None,
        "group_id": group_id or None,
        "confidence": confidence,
    }
    if kind == "plate":
        entry["plate"] = (
            _text(getattr(group, "matched_name", None))
            or _text(_attr(getattr(thumb, "attributes", None), "matchedName"))
            or None
        )
    return entry


def recognitions(event: Any) -> list[dict[str, Any]]:
    """Every face/plate recognised in one event, best first.

    De-duplicated per recognition group (one person caught in several crops is
    one entry, at their best confidence), then ordered known > unknown >
    unidentified and by confidence, so the card can just show the first entry.
    """
    thumbs = getattr(getattr(event, "metadata", None), "detected_thumbnails", None) or []
    best: dict[str, dict[str, Any]] = {}
    for index, thumb in enumerate(thumbs):
        entry = classify(thumb)
        if entry is None:
            continue
        key = entry["group_id"] or f"crop-{index}"
        if key not in best or _sort_key(entry) > _sort_key(best[key]):
            best[key] = entry
    return sorted(best.values(), key=_sort_key, reverse=True)


def _sort_key(entry: dict[str, Any]) -> tuple[int, float]:
    return (_RANK[entry["verdict"]], float(entry.get("confidence") or 0))
