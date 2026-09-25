# -*- coding: utf-8 -*-
"""Maintained details. The point is not storing them, but that they survive
everything that happens to the collected data."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.annotations import Annotation
from daedalus.model import Observation, Relation, Run, Source
from daedalus.reconcile import ingest

T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)

FDB = Source("fdb", frozenset({Relation.ATTACHMENT}), missing_threshold=2)


def attachment(d, p): return Observation(Relation.ATTACHMENT, d, "attachment", p)
def run(s, n, obs): return ingest(s, Run("fdb", t(n)), FDB, obs)


@pytest.fixture
def pg(store):
    if not hasattr(store, "annotations"):
        pytest.skip("annotations only exist with a database")
    return store


def test_outlet_on_a_port(pg):
    pg.annotations.write("c3po:gi12", outlet="B2-14", room="Office", changed_by="alex")
    a = pg.annotations.read("c3po:gi12")
    assert (a.outlet, a.room, a.changed_by) == ("B2-14", "Office", "alex")
    assert a.summary() == "outlet B2-14 · Office"


def test_an_outlet_can_be_entered_before_anything_is_plugged_in(pg):
    """You cable first and plug something in later."""
    pg.annotations.write("l337:gi9", outlet="K-03", room="Basement")
    assert pg.annotations.read("l337:gi9").outlet == "K-03"


def test_note_survives_the_device_disappearing(pg):
    """The core: collected data ends, maintained data does not."""
    run(pg, 0, [attachment("laptop", "c3po:gi12")])
    pg.annotations.write("laptop", note="belongs to the lab, do not unplug")
    run(pg, 1, [])
    run(pg, 2, [])                       # now it counts as disappeared
    assert pg.open_for(Relation.ATTACHMENT, "laptop", "attachment") is None
    assert pg.annotations.read("laptop").note == "belongs to the lab, do not unplug"


def test_and_is_still_there_when_it_comes_back(pg):
    run(pg, 0, [attachment("laptop", "c3po:gi12")])
    pg.annotations.write("laptop", note="lab")
    run(pg, 1, []); run(pg, 2, [])
    run(pg, 3, [attachment("laptop", "c3po:gi16")])
    assert pg.annotations.read("laptop").note == "lab"


def test_a_collection_run_never_touches_annotations(pg):
    pg.annotations.write("c3po:gi12", outlet="B2-14")
    for n in range(4):
        run(pg, n, [attachment("laptop", "c3po:gi12")])
    assert pg.annotations.read("c3po:gi12").outlet == "B2-14"


def test_only_fields_passed_are_touched(pg):
    pg.annotations.write("c3po:gi12", outlet="B2-14", room="Office")
    pg.annotations.write("c3po:gi12", note="printer is here")
    a = pg.annotations.read("c3po:gi12")
    assert (a.outlet, a.room, a.note) == ("B2-14", "Office", "printer is here")


def test_empty_value_clears_the_field(pg):
    """"should go" and "none of my business" must be distinguishable."""
    pg.annotations.write("c3po:gi12", outlet="B2-14", room="Office")
    pg.annotations.write("c3po:gi12", outlet="   ")
    a = pg.annotations.read("c3po:gi12")
    assert a.outlet is None and a.room == "Office"


def test_everything_cleared_leaves_no_empty_shell(pg):
    pg.annotations.write("c3po:gi12", outlet="B2-14")
    pg.annotations.write("c3po:gi12", outlet="")
    assert pg.annotations.read("c3po:gi12").is_empty


def test_unknown_field_is_refused(pg):
    with pytest.raises(ValueError):
        pg.annotations.write("c3po:gi12", colour="blue")


def test_search_finds_outlet_and_room(pg):
    pg.annotations.write("c3po:gi12", outlet="B2-14", room="Office")
    pg.annotations.write("l337:gi1", outlet="K-03", room="Basement back")
    pg.annotations.write("k2so:gi2", note="wall outlet broken, wire 3 cut")

    assert [x for x, _ in pg.annotations.search("Basement")] == ["l337:gi1"]
    assert [x for x, _ in pg.annotations.search("B2-14")] == ["c3po:gi12"]
    assert [x for x, _ in pg.annotations.search("broken")] == ["k2so:gi2"]


def test_summary_stays_readable_if_only_one_is_maintained(pg):
    assert Annotation("x", outlet="B2-14").summary() == "outlet B2-14"
    assert Annotation("x", room="Basement").summary() == "Basement"
    assert Annotation("x").summary() == ""
