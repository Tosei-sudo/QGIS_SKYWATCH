"""
What found things continue (core/obs/continuation.py): near, of a like class
and length, and unconfused is sure; unsure ones are offered; other top
classes never match; one found thing per thing.
"""
from satellite_observation.core.obs.continuation import AUTO, CANDIDATE, NEW, Found, class_relation, continuations
from satellite_observation.core.obs.flows import CarryOver
from satellite_observation.core.obs.geo import offset
from satellite_observation.core.obs.model import Observation
from satellite_observation.core.obs.taxonomy import Taxon

import math

HERE = (139.7, 35.6)
TAXA = [Taxon(["船舶"]), Taxon(["船舶", "護衛艦"]), Taxon(["船舶", "支援船"]), Taxon(["航空機"])]


def at(metres: float, degrees: float = 90.0) -> tuple[float, float]:
    return offset(HERE, metres, math.radians(degrees))


def thing(name: str, kind: str, metres: float = 0.0, length=None, heading=None, degrees: float = 90.0) -> CarryOver:
    lon, lat = at(metres, degrees)
    return CarryOver(Observation(id=f"r-{name}", image_id="before", image_name="", image_time=1, object_id=f"o-{name}", object_name=name,
                                 kind=kind, activity="", present=True, confidence="", note="", lon=lon, lat=lat, created_at=1, updated_at=1,
                                 deleted_at=None, length=length, heading=heading), "before")


def found(metres: float, kind: str = "", length=None, heading=None, degrees: float = 90.0, **more) -> Found:
    lon, lat = at(metres, degrees)
    return Found(lon, lat, kind, length, heading, **more)


def test_how_classes_stand():
    assert class_relation("船舶", "船舶") == "same"
    assert class_relation("船舶", "船舶 > 護衛艦") == "related"
    assert class_relation("船舶 > 支援船", "船舶 > 護衛艦", TAXA) == "sibling"
    assert class_relation("船舶", "航空機", TAXA) == "other"
    # A class the classification does not know, or none: not against it.
    assert class_relation("ship", "航空機", TAXA) == "unknown"
    assert class_relation("", "航空機", TAXA) == "unknown"
    assert class_relation("ship", "航空機") == "other"


def test_near_alike_and_alone_is_sure():
    out = continuations([found(10, "船舶", 100), found(5000, "船舶")], [thing("あおば", "船舶 > 護衛艦", 0, 105)], TAXA)
    assert out[0].tier == AUTO and out[0].pick.source.object_name == "あおば"
    assert out[0].options[0].reasons == ["10 m", "同系の分類", "長さ ±5%"]
    assert out[1].tier == NEW and out[1].pick is None


def test_unsure_ones_are_offered():
    carried = [thing("あおば", "船舶 > 護衛艦", 0, 100)]
    # Farther than sure, an unknown class, another length.
    assert continuations([found(150, "船舶")], carried, TAXA)[0].tier == CANDIDATE
    assert continuations([found(10, "ship")], carried, TAXA)[0].tier == CANDIDATE
    assert continuations([found(10, "船舶", 40)], carried, TAXA)[0].tier == CANDIDATE
    # A long thing reaches farther: half its length is near.
    assert continuations([found(70, "船舶", 200)], [thing("ましゅう", "船舶", 0, 220)], TAXA)[0].tier == AUTO
    # Another top class: never.
    assert continuations([found(5, "航空機")], carried, TAXA)[0].tier == NEW
    # 0: never without asking.
    assert continuations([found(1, "船舶", radius=0)], carried, TAXA)[0].tier == CANDIDATE


def test_two_near_each_other_are_not_sure_and_each_thing_is_matched_once():
    two = [thing("A", "船舶", 0), thing("B", "船舶", 30, degrees=0)]
    out = continuations([found(5, "船舶")], two, TAXA)
    assert out[0].tier == CANDIDATE and [o.source.object_name for o in out[0].options] == ["A", "B"]
    # Two found by one thing: the nearer is matched first, neither is sure.
    out = continuations([found(20, "船舶"), found(3, "船舶")], [thing("A", "船舶", 0)], TAXA)
    assert [c.tier for c in out] == [CANDIDATE, CANDIDATE]
    # Two found by two things, each near its own.
    out = continuations([found(3, "船舶", degrees=180), found(500, "船舶")], [thing("A", "船舶", 0), thing("B", "船舶", 495)], TAXA)
    assert [(c.tier, c.pick.source.object_name) for c in out] == [(AUTO, "A"), (AUTO, "B")]
