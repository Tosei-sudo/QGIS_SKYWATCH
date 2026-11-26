"""
What found things continue (core/obs/continuation.py): near, of a like class
and length, and unconfused is sure; unsure ones are offered; other top
classes never match; one found thing per thing.
"""
from mikage.core.obs.continuation import AUTO, CANDIDATE, NEW, Found, class_relation, continuations
from mikage.core.obs.flows import CarryOver
from mikage.core.obs.geo import offset
from mikage.core.obs.model import Observation
from mikage.core.obs.taxonomy import Taxon

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


# Rules (aiContinuation, 2026-11-26): what is sure can be set.

from mikage.core.obs.continuation import Rule, Rules, _overlap, _xy, old_rules  # noqa: E402
from mikage.core.obs.flows import outline_of  # noqa: E402


def measured(name: str, kind: str, metres: float = 0.0, length=100.0, width=20.0, heading=90.0) -> CarryOver:
    """A thing recorded with its outline (3 or 4 points): along the east, 100 × 20 m."""
    c = thing(name, kind, metres, length, heading)
    c.source.width = width
    return c


def box(metres: float, length: float = 100.0, width: float = 20.0, heading: float = 90.0, degrees: float = 90.0) -> list:
    lon, lat = at(metres, degrees)
    return outline_of(lon, lat, length, width, heading)


def ruled(metres: float, kind: str = "船舶", *rules: Rule, ring=None, score=None, length=None, degrees: float = 90.0,
          candidate: float = 300.0, unique: bool = True) -> Found:
    return found(metres, kind, length, degrees=degrees, ring=ring, score=score, rules=Rules(list(rules), candidate, unique))


INSIDE = Rule(label="外形の中", center_in_record=True)


def test_the_old_rule_is_what_was_sure_before():
    rules = old_rules(80, 500)
    assert (rules.candidate_radius, len(rules.rules)) == (500, 1)
    r = rules.rules[0]
    assert (r.near, r.kind, r.length_gap, r.default) == (80, "related", 0.3, True)
    assert old_rules(0).rules == []
    out = continuations([found(10, "船舶", 100)], [thing("あおば", "船舶 > 護衛艦", 0, 105)], TAXA)
    # Its note says 「[自動継続]」 as it always did.
    assert out[0].tag == "自動継続" and out[0].pick.rule.default


def test_the_centre_of_the_ai_inside_a_things_outline_continues_it():
    carried = [measured("あおば", "船舶 > 護衛艦")]
    # 45 m along a 100 m thing: inside its outline (beyond the old 50 m rule's reach of a thing without a length: here none).
    out = continuations([ruled(45, "船舶", INSIDE, ring=box(45, 30, 10))], carried, TAXA)
    assert out[0].tier == AUTO and out[0].pick.rule is INSIDE and out[0].tag == "自動継続:外形の中"
    assert out[0].pick.text == "あおば（外形の中・45 m・同系の分類）"
    # The centre of the polygon, not the point given: the point inside, the polygon mostly beyond the end.
    out = continuations([ruled(45, "船舶", INSIDE, ring=box(80, 60, 10))], carried, TAXA)
    assert out[0].tier == CANDIDATE and out[0].options[0].rule is None
    # 15 m to the side of a thing 20 m wide: outside.
    assert continuations([ruled(15, "船舶", INSIDE, degrees=0)], carried, TAXA)[0].tier == CANDIDATE
    # A thing measured without its width has no outline.
    assert continuations([ruled(10, "船舶", INSIDE)], [thing("あおば", "船舶", 0, 100, 90)], TAXA)[0].tier == CANDIDATE


def test_the_thing_inside_the_ais_polygon_continues_it():
    rule = Rule(record_in_ai=True)
    carried = [thing("A", "船舶", 0)]
    assert continuations([ruled(30, "船舶", rule, ring=box(30, 80, 20))], carried, TAXA)[0].tier == AUTO
    assert continuations([ruled(60, "船舶", rule, ring=box(60, 80, 20))], carried, TAXA)[0].tier == CANDIDATE
    # A point found has no polygon.
    assert continuations([ruled(1, "船舶", rule)], carried, TAXA)[0].tier == CANDIDATE
    # Named by its conditions when it has no name of its own.
    assert rule.text == "AIの範囲の中"
    assert continuations([ruled(30, "船舶", rule, ring=box(30, 80, 20))], carried, TAXA)[0].tag == "自動継続:AIの範囲の中"


def test_how_much_outlines_overlap():
    origin = HERE
    a = [_xy(origin, p) for p in box(0)]
    assert abs(_overlap(a, a) - 1) < 1e-6
    # Half a length along: 50 × 20 shared of 150 × 20 together.
    half = [_xy(origin, p) for p in box(50)]
    assert abs(_overlap(half, a) - 1 / 3) < 0.01
    assert _overlap([_xy(origin, p) for p in box(500)], a) == 0
    rule = Rule(overlap=0.5)
    carried = [measured("A", "船舶")]
    out = continuations([ruled(5, "船舶", rule, ring=box(5))], carried, TAXA)
    assert out[0].tier == AUTO and "重なり 90%" in out[0].pick.reasons
    assert continuations([ruled(50, "船舶", rule, ring=box(50))], carried, TAXA)[0].tier == CANDIDATE


def test_the_kind_a_rule_asks_for():
    carried = [thing("A", "船舶 > 護衛艦", 0)]
    near = Rule(near=50)
    assert continuations([ruled(10, "船舶", near)], carried, TAXA)[0].tier == AUTO
    assert continuations([ruled(10, "船舶", Rule(near=50, kind="same"))], carried, TAXA)[0].tier == CANDIDATE
    assert continuations([ruled(10, "船舶 > 支援船", Rule(near=50, kind="related"))], carried, TAXA)[0].tier == CANDIDATE
    assert continuations([ruled(10, "船舶 > 支援船", Rule(near=50, kind="sibling"))], carried, TAXA)[0].tier == AUTO
    # A class the classification does not know: not against it unless asked.
    assert continuations([ruled(10, "ship", near)], carried, TAXA)[0].tier == AUTO
    assert continuations([ruled(10, "ship", Rule(near=50, kind="sibling"))], carried, TAXA)[0].tier == CANDIDATE
    # Another top class: never, unless the rule looks at no kind at all.
    assert continuations([ruled(10, "航空機", near)], carried, TAXA)[0].tier == NEW
    out = continuations([ruled(10, "航空機", Rule(near=50, kind="any"))], carried, TAXA)
    assert out[0].tier == AUTO and "別の大分類" in out[0].pick.reasons


def test_length_heading_and_score():
    carried = [thing("A", "船舶", 0, 100, 90)]
    assert continuations([ruled(10, "船舶", Rule(near=50, length_gap=0.1), length=120)], carried, TAXA)[0].tier == CANDIDATE
    assert continuations([ruled(10, "船舶", Rule(near=50, length_gap=0.3), length=120)], carried, TAXA)[0].tier == AUTO
    f = ruled(10, "船舶", Rule(near=50, heading_gap=30))
    f.heading = 180
    assert continuations([f], carried, TAXA)[0].tier == CANDIDATE
    f.heading = 100
    assert continuations([f], carried, TAXA)[0].tier == AUTO
    sure = Rule(near=50, min_score=0.8)
    assert continuations([ruled(10, "船舶", sure, score=0.5)], carried, TAXA)[0].tier == CANDIDATE
    assert continuations([ruled(10, "船舶", sure, score=0.9)], carried, TAXA)[0].tier == AUTO
    # A result without a score is not held back by it.
    assert continuations([ruled(10, "船舶", sure)], carried, TAXA)[0].tier == AUTO


def test_the_first_rule_that_holds_names_it():
    carried = [measured("A", "船舶")]
    near = Rule(label="近い", near=50)
    out = continuations([ruled(10, "船舶", INSIDE, near, ring=box(10, 30, 10))], carried, TAXA)
    assert out[0].pick.rule is INSIDE
    out = continuations([ruled(10, "船舶", near, INSIDE, ring=box(10, 30, 10))], carried, TAXA)
    assert out[0].pick.rule is near
    # The first does not hold (another kind than it asks for), the second does.
    out = continuations([ruled(10, "船舶 > 護衛艦", Rule(label="近い", near=50, kind="same"), INSIDE, ring=box(10, 30, 10))], carried, TAXA)
    assert out[0].pick.rule is INSIDE


def test_unconfused_offered_and_off():
    carried = [measured("A", "船舶")]
    # Two found inside one thing: neither is sure, unless confusion is allowed (the likelier then).
    two = [ruled(10, "船舶", INSIDE, ring=box(10, 20, 10)), ruled(30, "船舶", INSIDE, ring=box(30, 20, 10))]
    assert [c.tier for c in continuations(two, carried, TAXA)] == [CANDIDATE, CANDIDATE]
    for f in two:
        f.rules.unique = False
    out = continuations(two, carried, TAXA)
    assert [c.tier for c in out] == [AUTO, CANDIDATE] and out[1].options[0].object_id == "o-A"
    # candidateRadius 0: only what a rule holds for.
    assert continuations([ruled(80, "船舶", INSIDE, candidate=0)], carried, TAXA)[0].tier == NEW
    assert continuations([ruled(30, "船舶", INSIDE, candidate=0)], carried, TAXA)[0].tier == AUTO
    # Off: nothing is matched.
    f = ruled(10, "船舶", INSIDE)
    f.rules.on = False
    assert continuations([f], carried, TAXA)[0].tier == NEW


def test_a_rule_with_a_long_reach_finds_beyond_the_candidates():
    out = continuations([ruled(1500, "船舶", Rule(near=2000), candidate=300)], [thing("A", "船舶", 0)], TAXA)
    assert out[0].tier == AUTO and out[0].pick.reasons[0] == "1.5 km"
    assert continuations([ruled(2500, "船舶", Rule(near=2000), candidate=300)], [thing("A", "船舶", 0)], TAXA)[0].tier == NEW
