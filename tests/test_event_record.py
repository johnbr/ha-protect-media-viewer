"""get_event_record re-reads the event through the events LIST.

``events/{id}`` drops the recognition ``group`` off every detection crop (only
the list attaches it), so a saved clip would otherwise lose its plates. The
client is built against the conftest uiprotect stub; ``_api`` is a fake.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from custom_components.protect_media_viewer import protect

SINGLE = {
    "id": "ev1",
    "start": 1_791_990_000_000,
    "end": 1_791_990_012_000,
    "metadata": {"detectedThumbnails": [{"type": "vehicle"}]},
}
LISTED = {
    **SINGLE,
    "metadata": {"detectedThumbnails": [{"type": "vehicle", "group": {"id": "vehicle_LPR-1FSTSLA"}}]},
}


class FakeApi:
    def __init__(self, rows=None, error=None, has_list=True):
        self.calls = []
        self._rows, self._error = rows, error
        if not has_list:
            self.get_events_raw = None

    async def api_request(self, path, raise_exception=True):
        return dict(SINGLE) if path == "events/ev1" else None

    async def get_events_raw(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._rows


def _client(api):
    client = protect.ProtectClient("h", 443, "u", "p", False, None)
    client._api = api
    return client


def test_listed_row_wins_and_window_spans_the_event():
    api = FakeApi(rows=[{"id": "other"}, LISTED])
    row = asyncio.run(_client(api)._listed_row(dict(SINGLE)))
    assert row is LISTED
    (call,) = api.calls
    assert call["start"] == datetime.fromtimestamp(SINGLE["start"] / 1000 - 1, UTC)
    assert call["end"] == datetime.fromtimestamp(SINGLE["end"] / 1000 + 1, UTC)


def test_open_event_uses_its_start_for_both_ends():
    api = FakeApi(rows=[])
    asyncio.run(_client(api)._listed_row({**SINGLE, "end": None}))
    (call,) = api.calls
    assert call["end"] == datetime.fromtimestamp(SINGLE["start"] / 1000 + 1, UTC)


def test_not_listed_or_failed_or_old_uiprotect_falls_back():
    for api in (FakeApi(rows=[{"id": "other"}]), FakeApi(error=RuntimeError("429")), FakeApi(has_list=False)):
        assert asyncio.run(_client(api)._listed_row(dict(SINGLE))) is None


def test_get_event_record_uses_the_listed_row(monkeypatch):
    seen = []
    monkeypatch.setattr(protect, "create_from_unifi_dict", lambda raw, api=None: seen.append(raw) or raw)
    monkeypatch.setattr(protect.ProtectClient, "_event_dict", lambda self, ev, raw: raw)
    assert asyncio.run(_client(FakeApi(rows=[LISTED])).get_event_record("ev1")) is LISTED
    assert seen == [LISTED]
    assert asyncio.run(_client(FakeApi(rows=[])).get_event_record("ev1")) == SINGLE
    assert asyncio.run(_client(FakeApi(rows=[LISTED])).get_event_record("gone")) is None
