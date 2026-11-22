"""
判読の練習 (練習モードの段階 3): the exercises, their answers and how a try is
scored. Pure Python (no QGIS): the screens (ui/exercise_*.py) give the records
made while practising as `Mark`s and show the `Outcome`.

An exercise is one image (two for 変化を見つける), an area on it and the
things in that area an experienced interpreter checked (`Answer`s). The
person answers with the observation dock's usual operations (クリック記録,
種別・活動, 2〜4 点で測る, 継続・消失), so practising is practising the real work.

Kinds of exercise (`TYPES`) and what is scored (`ITEMS`):

* 見つける: every thing of the target kinds in the area recorded — found
  (and missed, and records of nothing: 誤り), its kind, its activity;
* 見分ける: the marked things' kind and activity (no searching);
* 測る: the marked things' length, width and heading (2〜4 点);
* 変化を見つける: against the image before — what came recorded, what was
  there before marked 継続 or 消失.

Scoring (`Scoring`, the settings file's `training.scoring`, each exercise
may change some of it): each answer is worth its points; of them, each item
that applies to it (`weights`, the items of its kind of exercise that the
answer says something about) takes its share. An answer found gets its
items' credits (kind: the same class 1, one level up 0.5, the top class only
0.2; a measure: full within the tolerance, less the farther out); one not
found gets nothing. The score is the points earned of the required answers'
(× 100), less `extraPenalty` for each record that is none of the answers,
from 0 to 100. Answers not required (加点) add when found and take nothing
when missed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional, Sequence

FIND, IDENTIFY, MEASURE, CHANGE = "見つける", "見分ける", "測る", "変化を見つける"
TYPES = (FIND, IDENTIFY, MEASURE, CHANGE)
#: Other words a table may say a kind of exercise with.
TYPE_WORDS = {"find": FIND, "identify": IDENTIFY, "measure": MEASURE, "change": CHANGE, "変化": CHANGE}
PRACTICE, EXAM = "練習", "試験"
MODE_WORDS = {"practice": PRACTICE, "exam": EXAM, "test": EXAM}
#: What changed between the image before and the exercise's image (変化を見つける).
CAME, STAYED, GONE = "来た", "継続", "消失"
CHANGES = (CAME, STAYED, GONE)
CHANGE_WORDS = {"new": CAME, "came": CAME, "arrived": CAME, "stayed": STAYED, "continued": STAYED, "gone": GONE, "left": GONE,
                "出現": CAME, "新規": CAME}
#: What is scored, by kind of exercise (in the order shown).
FIND_ITEM, KIND_ITEM, ACTIVITY_ITEM, MEASURE_ITEM, CHANGE_ITEM = "find", "kind", "activity", "measure", "change"
ITEMS = {FIND: (FIND_ITEM, KIND_ITEM, ACTIVITY_ITEM), IDENTIFY: (KIND_ITEM, ACTIVITY_ITEM), MEASURE: (MEASURE_ITEM,),
         CHANGE: (FIND_ITEM, CHANGE_ITEM)}
ITEM_NAMES = {FIND_ITEM: "見つける", KIND_ITEM: "種別", ACTIVITY_ITEM: "活動", MEASURE_ITEM: "測る", CHANGE_ITEM: "変化"}
#: How an answer came out (shown in the review).
RIGHT, WRONG_KIND, WRONG_ACTIVITY, OFF_MEASURE, WRONG_CHANGE, MISSED, MISSED_BONUS = (
    "正しい", "種別ちがい", "活動ちがい", "測るのずれ", "変化ちがい", "見落とし", "見落とし（加点）")
EXTRA = "誤り"
EARTH = 6_371_008.8
_SPLIT = re.compile(r"\s*[>›＞]\s*")


def path_of(kind: str) -> list[str]:
    """A kind's path (`船舶 > 貨物船`; also › and ＞ between), as the observation records write it."""
    return [s.strip() for s in _SPLIT.split(kind or "") if s.strip()]


def kind_text(kind: str) -> str:
    return " > ".join(path_of(kind))


def within(kind: str, target: str) -> bool:
    """Whether a kind is the target class or below it."""
    p, t = path_of(kind), path_of(target)
    return bool(t) and len(t) <= len(p) and p[:len(t)] == t


def metres(a: Sequence[float], b: Sequence[float]) -> float:
    """The distance (m) between two longitude / latitude places."""
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    return 2 * EARTH * math.asin(min(1.0, math.sqrt(h)))


def inside(lon: float, lat: float, rings: Sequence[Sequence[Sequence[float]]]) -> bool:
    """Whether a place is in a polygon (its rings: the first the outline, the rest holes; even-odd)."""
    hit = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            (x1, y1), (x2, y2) = ring[i][:2], ring[(i + 1) % n][:2]
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                hit = not hit
    return hit


# ---- the scoring's rules ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Scoring:
    #: A record this near an answer (m) is taken as it; or the thing's length × `tolerance_by_length` when more.
    tolerance: float = 50.0
    tolerance_by_length: float = 0.5
    #: Each item's share of an answer's points (those that apply to it only, made up to the whole).
    weights: tuple = ((FIND_ITEM, 50.0), (KIND_ITEM, 30.0), (ACTIVITY_ITEM, 10.0), (MEASURE_ITEM, 10.0), (CHANGE_ITEM, 50.0))
    #: The kind's credit: the same class, its class one level up only, the top class only.
    kind_exact: float = 1.0
    kind_parent: float = 0.5
    kind_top: float = 0.2
    #: Points (of 100) taken off for each record that is none of the answers.
    extra_penalty: float = 5.0
    #: A measure in this (length and width: %, heading: degrees) gets it all; out of it, less up to three times as far.
    length_tolerance: float = 15.0
    heading_tolerance: float = 15.0
    #: Whether the heading may be read either way (bow and stern not told apart: SAR).
    heading_either_end: bool = False
    #: The score (of 100) that passes.
    pass_score: float = 70.0

    def weight(self, item: str) -> float:
        return dict(self.weights).get(item, 0.0)


DEFAULT_SCORING = Scoring()
#: The settings' keys (`training.scoring` and an exercise's own) and their fields.
SCORING_KEYS = {"tolerance": "tolerance", "toleranceByLength": "tolerance_by_length", "weights": "weights", "kindCredit": "kind",
                "extraPenalty": "extra_penalty", "lengthTolerance": "length_tolerance", "headingTolerance": "heading_tolerance",
                "headingEitherEnd": "heading_either_end", "passScore": "pass_score"}
KIND_CREDIT_KEYS = {"exact": "kind_exact", "parent": "kind_parent", "top": "kind_top"}


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def scoring_of(value: Any, problems: list[str], at: str = "training.scoring", base: Scoring = DEFAULT_SCORING) -> Scoring:
    """
    The scoring's rules of a settings value (`{"tolerance": 50, "weights": {"find": 50, …}, …}`) over `base` (what is
    not given stays as it is): the organisation's over the plugin's, an exercise's over the organisation's.
    """
    if value is None or value == {} or value == "":
        return base
    if not isinstance(value, dict):
        problems.append(f"{at} は {{\"tolerance\": 50, \"weights\": {{…}}, …}} の形で指定")
        return base
    changes: dict[str, Any] = {}
    for key, v in value.items():
        name = SCORING_KEYS.get(key)
        if name is None:
            problems.append(f"{at} に不明なキー「{key}」（{'・'.join(SCORING_KEYS)}）")
        elif name == "weights":
            if not isinstance(v, dict):
                problems.append(f"{at}.weights は {{\"find\": 50, \"kind\": 30, …}} の形で指定")
                continue
            weights = dict(base.weights)
            for item, w in v.items():
                n = _number(w)
                if item not in ITEM_NAMES:
                    problems.append(f"{at}.weights に不明な項目「{item}」（{'・'.join(ITEM_NAMES)}）")
                elif n is None or n < 0:
                    problems.append(f"{at}.weights.{item} は 0 以上の数で指定")
                else:
                    weights[item] = n
            changes["weights"] = tuple(weights.items())
        elif name == "kind":
            if not isinstance(v, dict):
                problems.append(f"{at}.kindCredit は {{\"exact\": 1, \"parent\": 0.5, \"top\": 0.2}} の形で指定")
                continue
            for k, c in v.items():
                n = _number(c)
                if k not in KIND_CREDIT_KEYS:
                    problems.append(f"{at}.kindCredit に不明なキー「{k}」（{'・'.join(KIND_CREDIT_KEYS)}）")
                elif n is None or not 0 <= n <= 1:
                    problems.append(f"{at}.kindCredit.{k} は 0〜1 の数で指定")
                else:
                    changes[KIND_CREDIT_KEYS[k]] = n
        elif name == "heading_either_end":
            if isinstance(v, bool):
                changes[name] = v
            else:
                problems.append(f"{at}.{key} は true か false で指定")
        else:
            n = _number(v)
            if n is None or n < 0 or (name == "pass_score" and n > 100):
                problems.append(f"{at}.{key} は {'0〜100' if name == 'pass_score' else '0 以上'}の数で指定")
            else:
                changes[name] = n
    return replace(base, **changes)


# ---- exercises and their answers --------------------------------------------------------------------------

@dataclass
class Answer:
    """One thing of an exercise as it truly is (a row of TBL_TRAINING_ANSWER)."""
    lon: float
    lat: float
    kind: str = ""
    activity: str = ""
    length: Optional[float] = None
    width: Optional[float] = None
    #: Degrees from north, clockwise, stern to bow.
    heading: Optional[float] = None
    #: 変化を見つける: 来た, 継続 or 消失 (消失: where it was in the image before).
    change: str = ""
    points: float = 10.0
    #: How near a record must be (m); None: the scoring's.
    tolerance: Optional[float] = None
    #: False: found adds points (加点), missed takes none.
    required: bool = True
    explanation: str = ""
    #: A picture of such a thing (a file or URL).
    reference: str = ""
    #: Its number on the map and in the review (1, 2, …).
    number: int = 0

    def measured(self) -> bool:
        return any(v is not None for v in (self.length, self.width, self.heading))


@dataclass
class Exercise:
    """One exercise (a row of TBL_TRAINING_EXERCISE with its answers)."""
    id: str
    title: str
    type: str
    image_id: str
    #: The area (rings, longitude / latitude); [] : the whole image.
    area: list = field(default_factory=list)
    instruction: str = ""
    #: The image's file or URL when it is not in a catalog (else "": opened by its id).
    image_source: str = ""
    #: 変化を見つける: the image before (by its id, or its file).
    before_image_id: str = ""
    before_image_source: str = ""
    #: The classes to record (`船舶`: and what is below); [] : any.
    target_kinds: list = field(default_factory=list)
    difficulty: str = ""
    #: Minutes (None: no limit).
    time_limit: Optional[float] = None
    mode: str = PRACTICE
    #: Whether the answers and explanations are shown after (an exam may keep them).
    show_answers: bool = True
    scoring: Scoring = DEFAULT_SCORING
    course: str = ""
    order: int = 0
    #: The roles it is for (担当の符号); [] : everyone.
    roles: list = field(default_factory=list)
    answers: list = field(default_factory=list)
    #: Where it came from (the sample, or the organisation's table's label).
    source: str = ""

    @property
    def exam(self) -> bool:
        return self.mode == EXAM

    @property
    def pass_score(self) -> float:
        return self.scoring.pass_score

    @property
    def key(self) -> str:
        """Its key among the results (the source too: two sources may use the same ids)."""
        return f"{self.source}:{self.id}" if self.source else self.id

    def numbered(self) -> "Exercise":
        """With its answers numbered 1, 2, … where they are not (in their order)."""
        taken = {a.number for a in self.answers if a.number > 0}
        n = 0
        for a in self.answers:
            if a.number <= 0:
                n += 1
                while n in taken:
                    n += 1
                a.number = n
        self.answers.sort(key=lambda a: a.number)
        return self

    def target_text(self) -> str:
        return "・".join(kind_text(k) for k in self.target_kinds) if self.target_kinds else "すべて"

    def unit(self) -> str:
        """How the things are counted (隻 for ships, else 件)."""
        return "隻" if self.target_kinds and all(path_of(k)[:1] == ["船舶"] for k in self.target_kinds) else "件"

    def rules_text(self) -> str:
        s = self.scoring
        parts = []
        if MEASURE_ITEM in ITEMS.get(self.type, ()):
            parts.append(f"長さ ±{s.length_tolerance:g} %・向き ±{s.heading_tolerance:g} 度で満点")
        else:
            parts.append(f"位置 {s.tolerance:g} m まで")
        if self.type in (FIND, CHANGE) and s.extra_penalty:
            parts.append(f"誤り 1 件 −{s.extra_penalty:g} 点")
        parts.append(f"合格 {s.pass_score:g} 点")
        return "・".join(parts)


def fitted(exercise: Exercise, kinds: Iterable[str], activities: Optional[dict] = None) -> Exercise:
    """
    The exercise with its answers' kinds as the classes in use say them: an answer's class not among `kinds`
    (another organisation's classes) is taken as far up as it is ("" : not scored); an activity the class does not
    offer (`activities`: kind → its words) is not scored either.
    """
    known = {kind_text(k) for k in kinds}
    if not known:
        return exercise
    answers = []
    for a in exercise.answers:
        path = path_of(a.kind)
        while path and " > ".join(path) not in known:
            path = path[:-1]
        kind = " > ".join(path)
        activity = a.activity
        if activities is not None and activity:
            words = activities.get(kind)
            if words is not None and activity not in words:
                activity = ""
        answers.append(replace(a, kind=kind, activity=activity))
    return replace(exercise, answers=answers)


# ---- a try and its score ----------------------------------------------------------------------------------

@dataclass
class Mark:
    """A record made for the exercise (as the screens give it)."""
    id: str
    lon: float
    lat: float
    kind: str = ""
    activity: str = ""
    length: Optional[float] = None
    width: Optional[float] = None
    heading: Optional[float] = None
    #: 変化を見つける: 来た, 継続 or 消失 (as the records say: a thing of the image before carried on, or not there).
    change: str = ""
    image_id: str = ""


@dataclass
class Item:
    """How one answer came out."""
    answer: Answer
    mark: Optional[Mark]
    distance: Optional[float]
    #: Each item that applies: its credit (0〜1).
    credits: dict
    #: Of 100.
    earned: float
    possible: float
    state: str

    @property
    def found(self) -> bool:
        return self.mark is not None


@dataclass
class Outcome:
    """A try scored."""
    exercise: Exercise
    items: list
    extras: list
    #: Of 100, after the extras' penalty.
    score: float
    #: Each item: (earned, possible), of 100.
    parts: dict
    penalty: float

    @property
    def passed(self) -> bool:
        return self.score >= self.exercise.pass_score

    @property
    def found(self) -> int:
        return sum(1 for i in self.items if i.found)

    @property
    def missed(self) -> int:
        return sum(1 for i in self.items if not i.found and i.answer.required)

    @property
    def required(self) -> int:
        return sum(1 for i in self.items if i.answer.required)

    def kind_score(self) -> Optional[float]:
        """The kinds' part as a share (0〜1) of what it could be; None when kinds are not scored."""
        earned, possible = self.parts.get(KIND_ITEM, (0.0, 0.0))
        return earned / possible if possible else None


def tolerance_of(answer: Answer, scoring: Scoring) -> float:
    if answer.tolerance is not None and answer.tolerance > 0:
        return answer.tolerance
    by_length = (answer.length or 0) * scoring.tolerance_by_length
    return max(scoring.tolerance, by_length)


def kind_credit(answer: str, given: str, scoring: Scoring = DEFAULT_SCORING) -> float:
    """The kind's credit: the answer's class or below it 1; up to its class one level up only; the top class only."""
    a, g = path_of(answer), path_of(given)
    if not a:
        return 1.0
    common = 0
    for x, y in zip(a, g):
        if x != y:
            break
        common += 1
    if common >= len(a):
        return scoring.kind_exact
    if common == len(a) - 1 and common > 0:
        return scoring.kind_parent
    return scoring.kind_top if common >= 1 else 0.0


def _closeness(error: float, tol: float) -> float:
    if error <= tol:
        return 1.0
    return max(0.0, 1.0 - (error - tol) / (2 * tol)) if tol > 0 else 0.0


def heading_error(a: float, b: float, either_end: bool = False) -> float:
    d = abs((a - b + 180) % 360 - 180)
    return min(d, 180 - d) if either_end else d


def measure_credit(answer: Answer, mark: Mark, scoring: Scoring = DEFAULT_SCORING) -> float:
    """The measure's credit: each of length, width and heading the answer has, by how far off (not measured: 0)."""
    parts = []
    for truth, given in ((answer.length, mark.length), (answer.width, mark.width)):
        if truth is not None and truth > 0:
            parts.append(0.0 if given is None else _closeness(abs(given - truth) / truth * 100, scoring.length_tolerance))
    if answer.heading is not None:
        parts.append(0.0 if mark.heading is None else
                     _closeness(heading_error(answer.heading, mark.heading, scoring.heading_either_end), scoring.heading_tolerance))
    return sum(parts) / len(parts) if parts else 1.0


def applies(exercise_type: str, answer: Answer, scoring: Scoring) -> list[str]:
    """The items an answer is scored on (its kind of exercise's, that it says something about, with a weight)."""
    out = []
    for item in ITEMS.get(exercise_type, ()):
        if item == KIND_ITEM and not answer.kind or item == ACTIVITY_ITEM and not answer.activity:
            continue
        if item == MEASURE_ITEM and not answer.measured() or item == CHANGE_ITEM and not answer.change:
            continue
        if scoring.weight(item) > 0:
            out.append(item)
    return out


def match(answers: Sequence[Answer], marks: Sequence[Mark], scoring: Scoring) -> dict:
    """Each answer's record (the nearest first, one record each), within its tolerance: answer index → (mark, m)."""
    pairs = []
    for i, a in enumerate(answers):
        tol = tolerance_of(a, scoring)
        for j, m in enumerate(marks):
            d = metres((a.lon, a.lat), (m.lon, m.lat))
            if d <= tol:
                pairs.append((d, i, j))
    pairs.sort()
    taken_a, taken_m, out = set(), set(), {}
    for d, i, j in pairs:
        if i in taken_a or j in taken_m:
            continue
        taken_a.add(i)
        taken_m.add(j)
        out[i] = (marks[j], d)
    return out


def score(exercise: Exercise, marks: Sequence[Mark]) -> Outcome:
    """
    A try scored: the records of the exercise's image (`marks`; 変化を見つける: of its image, the one after) against
    the answers. Records outside the area that are none of the answers, and records of other classes than the
    targets, are not counted as 誤り.
    """
    s = exercise.scoring
    answers = exercise.answers
    pairs = match(answers, marks, s)
    possible_total = sum(a.points for a in answers if a.required) or 1.0
    scale = 100.0 / possible_total
    items, parts = [], {item: [0.0, 0.0] for item in ITEMS.get(exercise.type, ())}
    for i, a in enumerate(answers):
        mark, distance = pairs.get(i, (None, None))
        used = applies(exercise.type, a, s)
        total_weight = sum(s.weight(x) for x in used)
        credits: dict[str, float] = {}
        for x in used:
            if mark is None:
                credits[x] = 0.0
            elif x == FIND_ITEM:
                credits[x] = 1.0
            elif x == KIND_ITEM:
                credits[x] = kind_credit(a.kind, mark.kind, s)
            elif x == ACTIVITY_ITEM:
                credits[x] = 1.0 if mark.activity.strip() == a.activity.strip() else 0.0
            elif x == MEASURE_ITEM:
                credits[x] = measure_credit(a, mark, s)
            elif x == CHANGE_ITEM:
                credits[x] = 1.0 if mark.change == a.change else 0.0
        earned = possible = 0.0
        for x in used:
            share = a.points * scale * s.weight(x) / total_weight
            if a.required:
                parts[x][1] += share
                possible += share
            parts[x][0] += share * credits[x]
            earned += share * credits[x]
        if mark is None:
            state = MISSED if a.required else MISSED_BONUS
        elif credits.get(KIND_ITEM, 1.0) < 1.0:
            state = WRONG_KIND
        elif credits.get(ACTIVITY_ITEM, 1.0) < 1.0:
            state = WRONG_ACTIVITY
        elif credits.get(MEASURE_ITEM, 1.0) < 1.0:
            state = OFF_MEASURE
        elif credits.get(CHANGE_ITEM, 1.0) < 1.0:
            state = WRONG_CHANGE
        else:
            state = RIGHT
        items.append(Item(a, mark, distance, credits, earned, possible, state))
    taken = {id(m) for m, _d in pairs.values()}
    extras = []
    if exercise.type in (FIND, CHANGE):
        for m in marks:
            if id(m) in taken:
                continue
            if exercise.area and not inside(m.lon, m.lat, exercise.area):
                continue
            if exercise.target_kinds and m.kind and not any(within(m.kind, t) for t in exercise.target_kinds):
                continue
            extras.append(m)
    penalty = len(extras) * s.extra_penalty
    total = sum(i.earned for i in items)
    value = max(0.0, min(100.0, total - penalty))
    # (a part's bonus answers fill what it missed; not past its whole)
    shown = {k: (round(min(v[0], v[1]), 1), round(v[1], 1)) for k, v in parts.items()}
    return Outcome(exercise, items, extras, round(value, 1), shown, round(penalty, 1))


def hint(exercise: Exercise, marks: Sequence[Mark]) -> str:
    """練習's hint: how many of the things to record are not yet (an exam has none)."""
    pairs = match(exercise.answers, marks, exercise.scoring)
    left = sum(1 for i, a in enumerate(exercise.answers) if a.required and i not in pairs)
    unit = exercise.unit()
    if exercise.type == IDENTIFY:
        return f"印のあるモノは {len(exercise.answers)} {unit}。まだ記録していないのは {left} {unit}です。" if left else "印のあるモノはすべて記録しました。"
    if exercise.type == MEASURE:
        return f"印のあるモノは {len(exercise.answers)} {unit}。まだ測っていないのは {left} {unit}です。" if left else "印のあるモノはすべて記録しました。"
    if not left:
        return "記録するものは、すべて見つかっています。種別・活動を見直して提出しましょう。"
    return f"あと {left} {unit}あります。"
