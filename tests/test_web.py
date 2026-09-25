# -*- coding: utf-8 -*-
"""HTTP contract of the canvas, without a running database."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from daedalus.model import Interval, Relation
from daedalus.store import MemoryStore
from daedalus.web import build_app


NOW = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


class AnnotationsDouble:
    def __init__(self) -> None:
        self.data = {"c3po:gi12": {"room": "Office"}}
        self.writes = []

    def all(self):
        return self.data.copy()

    def read(self, key):
        return self.data.get(key, {})

    def write(self, key, changed_by="", **fields):
        self.writes.append((key, changed_by, fields))
        self.data[key] = {k: v for k, v in fields.items() if v}
        return self.data[key]


class StoreDouble(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.annotations = AnnotationsDouble()

    def sightings(self):
        return {"aa:bb:cc:dd:ee:ff": (NOW, NOW)}


def _store() -> StoreDouble:
    store = StoreDouble()
    store.intervals.extend([
        Interval(Relation.ADDRESS, "aa:bb:cc:dd:ee:ff", "address",
                 "172.16.10.44", NOW),
        Interval(Relation.ATTACHMENT, "aa:bb:cc:dd:ee:ff", "attachment",
                 "c3po:gi12", NOW),
        # The string proves that payload data cannot end the JSON script block and
        # still arrives unchanged after JSON.parse.
        Interval(Relation.ATTRIBUTE, "aa:bb:cc:dd:ee:ff", "name",
                 "Sensor </script> Lab", NOW),
    ])
    return store


def _client():
    store = _store()
    app = build_app(lambda: store, store_factory=lambda db: db, root="bb8")
    return TestClient(app), store


def test_start_page_embeds_a_parseable_and_safe_state():
    client, _ = _client()
    response = client.get("/")

    assert response.status_code == 200
    m = re.search(
        r'<script id="state" type="application/json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    assert m
    assert "<\\/script>" in m.group(1)
    state = json.loads(m.group(1))
    assert state["DEV"][0]["label"] == "Sensor </script> Lab"


def test_api_state_has_the_expected_shape():
    client, _ = _client()
    state = client.get("/api/state").json()

    assert state["root"] == "bb8"
    assert state["DEV"][0]["id"] == "aa:bb:cc:dd:ee:ff"
    assert {"NETS", "APS", "SWITCHES", "PORTS", "CHANGES", "ANNOTATIONS"} <= set(state)


def test_annotation_port_key_and_user_arrive_correctly():
    client, store = _client()
    response = client.put(
        "/api/annotation/c3po:gi12",
        json={"outlet": "B2-14"},
        headers={"X-Forwarded-Preferred-Username": "alex"},
    )

    assert response.status_code == 200
    assert store.annotations.writes[-1] == (
        "c3po:gi12", "alex", {"outlet": "B2-14"},
    )


def test_annotation_network_key_with_slash_arrives_correctly():
    client, store = _client()
    response = client.put(
        "/api/annotation/net:172.16.10.0/24",
        json={"note": "internal"},
        headers={"X-Forwarded-User": "proxy-user"},
    )

    assert response.status_code == 200
    assert store.annotations.writes[-1] == (
        "net:172.16.10.0/24", "proxy-user", {"note": "internal"},
    )


def test_annotation_unknown_field_is_invalid():
    client, _ = _client()
    response = client.put("/api/annotation/c3po:gi12", json={"colour": "blue"})
    assert response.status_code == 422


def test_health_needs_no_database():
    app = build_app(lambda: (_ for _ in ()).throw(RuntimeError("no db")), root="bb8")
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
