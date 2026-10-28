"""Tests for recognition.py: face names and plates from detection crops.

Fixtures mirror real UFP thumbnails seen on 2026-10-02 (group ids and
confidences verbatim; names and plates made up), shaped the way uiprotect parses them.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from custom_components.protect_media_viewer.recognition import classify, recognitions


def face(group_id, name=None, confidence=60):
    return NS(
        type="face",
        confidence=confidence,
        attributes=None,
        group=NS(id=group_id, name=name, matched_name=name or "", confidence=confidence),
    )


def plate(text, label=None, confidence=89):
    return NS(
        type="vehicle",
        confidence=90,
        attributes={"matchedName": text},
        group=NS(id=f"vehicle_LPR-{text}", name=label, matched_name=text, confidence=confidence),
    )


def event(*thumbs):
    return NS(metadata=NS(detected_thumbnails=list(thumbs)))


ALEX = face("face_18", "Alex", 80)
UNNAMED = face("face_23", confidence=95)
DEGRADED = face("face_degraded_697534d0-0f4c-41ae-8d01-89cc353fd50e", confidence=60)


def test_known_face():
    e = classify(ALEX)
    assert (e["kind"], e["verdict"], e["name"], e["confidence"]) == ("face", "known", "Alex", 80)


def test_unnamed_group_is_unknown_not_unidentified():
    assert classify(UNNAMED)["verdict"] == "unknown"
    assert classify(UNNAMED)["name"] is None


def test_degraded_face_is_unidentified():
    assert classify(DEGRADED)["verdict"] == "unidentified"


def test_known_plate_keeps_label_and_text():
    e = classify(plate("7ABC123", "Sedan"))
    assert (e["kind"], e["verdict"], e["name"], e["plate"]) == ("plate", "known", "Sedan", "7ABC123")


def test_unlabelled_plate_is_unknown():
    e = classify(plate("4XYZ567", confidence=88))
    assert (e["verdict"], e["name"], e["plate"], e["confidence"]) == ("unknown", None, "4XYZ567", 88)


def test_vehicle_without_plate_and_person_are_ignored():
    assert classify(NS(type="vehicle", group=None, confidence=91, attributes=None)) is None
    assert classify(NS(type="person", group=None)) is None


def test_best_first_and_deduplicated():
    weak_alex = face("face_18", "Alex", 55)
    out = recognitions(event(DEGRADED, weak_alex, ALEX, plate("5DEF890", "Hatchback", 90)))
    assert [(r["name"], r["confidence"]) for r in out[:2]] == [("Hatchback", 90), ("Alex", 80)]
    assert [r["verdict"] for r in out] == ["known", "known", "unidentified"]


def test_unknown_outranks_unidentified():
    assert recognitions(event(DEGRADED, UNNAMED))[0]["verdict"] == "unknown"


def test_event_without_metadata():
    assert recognitions(NS(metadata=None)) == []
    assert recognitions(NS()) == []
