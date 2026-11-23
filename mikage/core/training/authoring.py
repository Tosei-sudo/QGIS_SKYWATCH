"""
判読の練習の問題づくり (練習モードの段階 3 の 3): the organisation's exercises
as their tables have them, those not offered yet (ACTIVE 0: 作りかけ) too
(`Draft`), changed by an instructor (「判読の練習の問題を作る…」, in the
production MIKAGE) and written back: the exercise's row added or changed,
its answers' rows added, changed and deleted by their object ids (who and
when stamped as every edit of MIKAGE is). The answers can be taken from
observation records of the image (an experienced interpreter's).

Pure Python: the rows are Esri JSON features (geometry in WGS 84) and
`client` an EsriClient (in a task); a draft is checked (`check`) and tried as
the practice will read it (`exercise_of_draft`: the same reading as the
table's rows).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

from ..esri import EsriError, fit_value
from ..shapes import esri_rings
from .exercises import (
    Answer, CAME, CHANGE, CHANGES, DEFAULT_SCORING, EXAM, Exercise, FIND, GONE, IDENTIFY, inside, MEASURE, MODE_WORDS, PRACTICE, Scoring,
    STAYED, TYPE_WORDS, TYPES, within,
)
from .tables import _flag, _number, _rings, _text, _words, answer_of, exercise_of, ExerciseTables, SOURCE

@dataclass
class DraftAnswer(Answer):
    """An answer of a draft, with its row's object id (None: not written yet)."""
    object_id: Optional[int] = None


@dataclass
class Draft:
    """An exercise as its row has it (ACTIVE or not), with its answers, to change and write back."""
    id: str
    title: str = ""
    type: str = FIND
    instruction: str = ""
    image_id: str = ""
    image_source: str = ""
    before_image_id: str = ""
    before_image_source: str = ""
    #: Rings (longitude / latitude); [] : the whole image.
    area: list = field(default_factory=list)
    target_kinds: list = field(default_factory=list)
    difficulty: str = ""
    #: Minutes (None: no limit).
    time_limit: Optional[float] = None
    mode: str = PRACTICE
    show_answers: bool = True
    #: None: the scoring's (the organisation's).
    pass_score: Optional[float] = None
    #: The exercise's own scoring (SCORING: training.scoring's keys, those it changes); {} : the organisation's.
    scoring: dict = field(default_factory=dict)
    course: str = ""
    order: int = 0
    roles: list = field(default_factory=list)
    #: False: 作りかけ (not offered).
    active: bool = True
    answers: list = field(default_factory=list)
    #: Its row's object id (None: not written yet), and its answers' rows' as read (those not kept are deleted).
    object_id: Optional[int] = None
    answer_ids_read: tuple = ()

    @property
    def key(self) -> str:
        return f"{SOURCE}:{self.id}"


# ---- reading ------------------------------------------------------------------------------------------------

def _oid(attributes: dict, name: str) -> Optional[int]:
    v = attributes.get(name)
    if v is None:
        low = name.lower()
        v = next((x for k, x in attributes.items() if k.lower() == low), None)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def draft_of(feature: dict, oid_field: str = "OBJECTID") -> Optional[Draft]:
    """A TBL_TRAINING_EXERCISE row as it is (None without its EXERCISE_ID); values not usable are left empty."""
    a = feature.get("attributes") or {}
    eid = _text(a, "EXERCISE_ID")
    if not eid:
        return None
    ignored: list[str] = []
    words = _text(a, "EXERCISE_TYPE")
    kind = words if words in TYPES else TYPE_WORDS.get(words.lower(), FIND)
    scoring: dict = {}
    text = _text(a, "SCORING")
    if text:
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        scoring = value if isinstance(value, dict) else {}
    limit = _number(a, "TIME_LIMIT_MIN", ignored, "")
    order = _number(a, "COURSE_ORDER", ignored, "")
    mode = _text(a, "EXERCISE_MODE")
    return Draft(
        eid, title=_text(a, "TITLE"), type=kind, instruction=_text(a, "INSTRUCTION"), image_id=_text(a, "IMAGE_ID"),
        image_source=_text(a, "IMAGE_SOURCE"), before_image_id=_text(a, "BEFORE_IMAGE_ID"), before_image_source=_text(a, "BEFORE_IMAGE_SOURCE"),
        area=_rings(feature.get("geometry")), target_kinds=_words(a, "TARGET_KINDS"), difficulty=_text(a, "DIFFICULTY"),
        time_limit=limit if limit is not None and limit > 0 else None,
        mode=EXAM if mode == EXAM or MODE_WORDS.get(mode.lower()) == EXAM else PRACTICE,
        show_answers=_flag(a, "SHOW_ANSWERS", True, ignored, ""), pass_score=_number(a, "PASS_SCORE", ignored, ""), scoring=scoring,
        course=_text(a, "COURSE_NAME"), order=int(order) if order is not None else 0, roles=_words(a, "ROLES"),
        active=_flag(a, "ACTIVE", True, ignored, ""), object_id=_oid(a, oid_field))


def draft_answer_of(feature: dict, oid_field: str = "OBJECTID") -> Optional[tuple[str, DraftAnswer]]:
    """(its exercise's id, the answer) of a TBL_TRAINING_ANSWER row, with its object id; None when not usable."""
    got = answer_of(feature, [])
    if got is None:
        return None
    eid, answer = got
    return eid, DraftAnswer(**{f: getattr(answer, f) for f in Answer.__dataclass_fields__},
                            object_id=_oid(feature.get("attributes") or {}, oid_field))


def drafts_of(exercise_rows: Iterable[dict], answer_rows: Iterable[dict], exercise_oid: str = "OBJECTID",
              answer_oid: str = "OBJECTID") -> list[Draft]:
    """Every exercise of the tables (作りかけ too) with its answers, by course (as the rows have them) and order."""
    answers: dict[str, list[DraftAnswer]] = {}
    for row in answer_rows:
        got = draft_answer_of(row, answer_oid)
        if got is not None:
            answers.setdefault(got[0], []).append(got[1])
    out, seen = [], set()
    for row in exercise_rows:
        d = draft_of(row, exercise_oid)
        if d is None or d.id in seen:
            continue
        seen.add(d.id)
        mine = sorted(answers.get(d.id, []), key=lambda x: (x.number <= 0, x.number))
        n = 0
        for x in mine:
            n += 1
            x.number = n
        d.answers = mine
        d.answer_ids_read = tuple(x.object_id for x in mine if x.object_id is not None)
        out.append(d)
    courses = {c: i for i, c in enumerate(dict.fromkeys(d.course for d in out))}
    return sorted(out, key=lambda d: (courses[d.course], d.order, d.title))


def read_drafts(client: Any, tables: ExerciseTables, token: Optional[str], cancelled=None) -> list[Draft]:
    """Every exercise of the organisation's tables with its answers (in a task)."""
    exercise_oid = client.layer(tables.exercises, token).object_id_field
    answer_oid = client.layer(tables.answers, token).object_id_field
    exercises = client.query_all(tables.exercises, "1=1", token, cancelled=cancelled, limit=20_000)
    answers = client.query_all(tables.answers, "1=1", token, cancelled=cancelled, limit=200_000)
    return drafts_of(exercises, answers, exercise_oid, answer_oid)


# ---- as the tables have it, and as the practice reads it -------------------------------------------------------

def _clean(v: Optional[float]) -> Optional[float]:
    return None if v is None else (int(v) if float(v).is_integer() else v)


def exercise_attributes(d: Draft) -> dict:
    """A draft as TBL_TRAINING_EXERCISE's values (tables.EXERCISE_FIELDS)."""
    return {
        "EXERCISE_ID": d.id, "TITLE": d.title.strip(), "INSTRUCTION": d.instruction.strip(), "EXERCISE_TYPE": d.type,
        "IMAGE_ID": d.image_id.strip() or None, "IMAGE_SOURCE": d.image_source.strip() or None,
        "BEFORE_IMAGE_ID": (d.before_image_id.strip() or None) if d.type == CHANGE else None,
        "BEFORE_IMAGE_SOURCE": (d.before_image_source.strip() or None) if d.type == CHANGE else None,
        "TARGET_KINDS": "; ".join(k.strip() for k in d.target_kinds if k.strip()) or None, "DIFFICULTY": d.difficulty.strip() or None,
        "TIME_LIMIT_MIN": _clean(d.time_limit) if d.time_limit else None, "EXERCISE_MODE": d.mode, "SHOW_ANSWERS": 1 if d.show_answers else 0,
        "PASS_SCORE": _clean(d.pass_score), "SCORING": json.dumps(d.scoring, ensure_ascii=False) if d.scoring else None,
        "COURSE_NAME": d.course.strip() or None, "COURSE_ORDER": int(d.order), "ROLES": ",".join(d.roles) or None,
        "ACTIVE": 1 if d.active else 0,
    }


def answer_attributes(eid: str, a: Answer) -> dict:
    """An answer as TBL_TRAINING_ANSWER's values (tables.ANSWER_FIELDS)."""
    return {
        "EXERCISE_ID": eid, "ANSWER_NO": int(a.number) if a.number > 0 else None, "KIND": a.kind.strip() or None,
        "ACTIVITY": a.activity.strip() or None, "LENGTH_M": _clean(a.length), "WIDTH_M": _clean(a.width), "HEADING": _clean(a.heading),
        "CHANGE_KIND": a.change or None, "ANSWER_POINTS": _clean(a.points), "TOLERANCE_M": _clean(a.tolerance),
        "REQUIRED": 1 if a.required else 0, "EXPLANATION": a.explanation.strip() or None, "REFERENCE_IMAGE": a.reference.strip() or None,
    }


def area_geometry(rings: Sequence) -> Optional[dict]:
    return {"rings": esri_rings([[list(p[:2]) for p in r] for r in rings]), "spatialReference": {"wkid": 4326}} if rings else None


def numbered(d: Draft) -> Draft:
    """The draft with its answers numbered 1, 2, … in their order (as written)."""
    for i, a in enumerate(d.answers):
        a.number = i + 1
    return d


def exercise_of_draft(d: Draft, base: Scoring = DEFAULT_SCORING, problems: Optional[list[str]] = None) -> Optional[Exercise]:
    """
    The exercise the practice will read of the draft once written (the same reading as the tables' rows: what it
    cannot use is left out, with why in `problems`), offered or not.
    """
    problems = problems if problems is not None else []
    attributes = {**exercise_attributes(d), "ACTIVE": 1}
    e = exercise_of({"attributes": attributes, "geometry": area_geometry(d.area)}, base, problems)
    if e is None:
        return None
    answers = []
    for i, a in enumerate(d.answers):
        got = answer_of({"attributes": {**answer_attributes(d.id, a), "ANSWER_NO": i + 1},
                         "geometry": {"x": a.lon, "y": a.lat}}, problems)
        if got is not None:
            answers.append(got[1])
    if not answers:
        problems.append("正解なし")
        return None
    e.answers = answers
    return e.numbered()


def check(d: Draft, base: Scoring = DEFAULT_SCORING) -> tuple[list[str], list[str]]:
    """
    What keeps a draft from being written (it would not be usable), and what is worth a second look (written all
    the same).
    """
    problems, notes = [], []
    if not d.title.strip():
        problems.append("題名なし")
    if not d.image_id.strip() and not d.image_source.strip():
        problems.append("画像なし（画像 ID か画像のファイルを指定）")
    if d.type == CHANGE and not d.before_image_id.strip() and not d.before_image_source.strip():
        problems.append("変化を見つける問題に前の画像なし")
    if not d.answers:
        problems.append("正解なし（観測記録から取り込むか、地図で追加）")
    if d.pass_score is not None and not 0 <= d.pass_score <= 100:
        problems.append("合格点は 0〜100")
    # (numbered as they are listed: as they are written)
    for n, a in enumerate(d.answers, 1):
        if d.type == MEASURE and not a.measured():
            problems.append(f"正解 {n}: 測る問題の正解に長さ・幅・向きなし")
        if d.type == CHANGE and a.change not in CHANGES:
            problems.append(f"正解 {n}: 変化（{'・'.join(CHANGES)}）なし")
        if d.type == IDENTIFY and not a.kind and not a.activity:
            problems.append(f"正解 {n}: 見分ける問題の正解に種別も活動もない")
    if d.area and d.type in (FIND, CHANGE):
        out = [n for n, a in enumerate(d.answers, 1) if not inside(a.lon, a.lat, d.area)]
        if out:
            notes.append(f"範囲の外の正解あり（{'・'.join(str(n) for n in out)} 番）")
    if not d.area and d.type in (FIND, CHANGE):
        notes.append("範囲なし（画像の全体を探す問題になる）")
    if d.target_kinds and d.type == FIND:
        odd = [n for n, a in enumerate(d.answers, 1) if a.kind and not any(within(a.kind, t) for t in d.target_kinds)]
        if odd:
            notes.append(f"対象の種別に入らない正解あり（{'・'.join(str(n) for n in odd)} 番）")
    if not d.active:
        notes.append("「出す」に印がないため、作りかけとして保存（練習の一覧には出ない）")
    tried: list[str] = []
    if not problems:
        exercise_of_draft(d, base, tried)
        problems.extend(p for p in tried if p not in problems)
    return problems, notes


def new_exercise_id(now: datetime, taken: Iterable[str] = ()) -> str:
    """A new exercise's id (`EX-20261122-153012`; `-2`… when taken)."""
    taken = set(taken)
    base = f"EX-{now:%Y%m%d-%H%M%S}"
    eid, n = base, 1
    while eid in taken:
        n += 1
        eid = f"{base}-{n}"
    return eid


def copied(d: Draft, eid: str) -> Draft:
    """A copy of a draft to make another from (new id, not written, 作りかけ)."""
    answers = [replace(a, object_id=None) for a in d.answers]
    return replace(d, id=eid, title=d.title + "（コピー）", answers=answers, object_id=None, answer_ids_read=(), active=False,
                   order=d.order + 1)


# ---- writing (in a task) --------------------------------------------------------------------------------------

def _fitted(attributes: dict, info: Any) -> dict:
    """The values by the table's own column names (any case), those it has only, texts cut to their length."""
    names = {n.lower(): n for n in info.field_types}
    out = {}
    for k, v in attributes.items():
        name = names.get(k.lower())
        if name is None:
            continue
        length = info.field_lengths.get(name)
        if isinstance(v, str) and length:
            v = v[:length]
        out[name] = fit_value(v, info.field_types.get(name))
    return out


def save_draft(client: Any, tables: ExerciseTables, d: Draft, token: Optional[str]) -> Draft:
    """
    A draft written: its row added (a new one) or changed, its answers' rows added, changed, and those of before
    not kept deleted. The draft as written (its object ids). Raises EsriError with why (what went in stays).
    """
    d = numbered(d)
    ex_info = client.layer(tables.exercises, token, refresh=True)
    row = {"attributes": _fitted(exercise_attributes(d), ex_info)}
    geometry = area_geometry(d.area)
    if geometry is not None:
        row["geometry"] = geometry
    if d.object_id is None:
        result = client.apply_edits(tables.exercises, adds=[row], token=token)
        adds = result.add_results
        if not adds or not adds[0].get("success"):
            raise EsriError(code="E305", detail="問題の表への追加: " + "、".join(result.failures()[:3]))
        object_id = adds[0].get("objectId")
    else:
        row["attributes"][ex_info.object_id_field] = d.object_id
        result = client.apply_edits(tables.exercises, updates=[row], token=token)
        ups = result.update_results
        if not ups or not ups[0].get("success"):
            raise EsriError(code="E305", detail="問題の表の更新: " + "、".join(result.failures()[:3]))
        object_id = d.object_id
    an_info = client.layer(tables.answers, token, refresh=True)
    oid = an_info.object_id_field
    adds, updates, new = [], [], []
    for a in d.answers:
        feature = {"attributes": _fitted(answer_attributes(d.id, a), an_info),
                   "geometry": {"x": a.lon, "y": a.lat, "spatialReference": {"wkid": 4326}}}
        if a.object_id is not None and a.object_id in d.answer_ids_read:
            feature["attributes"][oid] = a.object_id
            updates.append(feature)
        else:
            adds.append(feature)
            new.append(a)
    kept = {a.object_id for a in d.answers if a.object_id is not None}
    deletes = [i for i in d.answer_ids_read if i not in kept]
    answers = [replace(a) for a in d.answers]
    if adds or updates or deletes:
        result = client.apply_edits(tables.answers, adds=adds, updates=updates, deletes=deletes, token=token)
        for a, r in zip(new, result.add_results):
            if r.get("success"):
                a.object_id = r.get("objectId")
        failures = result.failures()
        if failures:
            raise EsriError("正解の表に保存できない行あり。もう一度保存", code="E305", detail="、".join(failures[:3]))
        answers = d.answers
    return replace(d, object_id=int(object_id) if object_id is not None else None, answers=answers,
                   answer_ids_read=tuple(a.object_id for a in answers if a.object_id is not None))


def set_active(client: Any, tables: ExerciseTables, d: Draft, active: bool, token: Optional[str]) -> Draft:
    """「出す」 changed (only that column)."""
    if d.object_id is None:
        return replace(d, active=active)
    info = client.layer(tables.exercises, token)
    attributes = _fitted({"ACTIVE": 1 if active else 0}, info)
    attributes[info.object_id_field] = d.object_id
    result = client.apply_edits(tables.exercises, updates=[{"attributes": attributes}], token=token)
    if result.failures() or not result.update_results:
        raise EsriError(code="E305", detail="問題の表の更新: " + "、".join(result.failures()[:3]))
    return replace(d, active=active)


def delete_draft(client: Any, tables: ExerciseTables, d: Draft, token: Optional[str]) -> None:
    """An exercise and its answers deleted from the tables (its results stay: the tries were made)."""
    if d.answer_ids_read:
        result = client.apply_edits(tables.answers, deletes=list(d.answer_ids_read), token=token)
        if result.failures():
            raise EsriError(code="E305", detail="正解の行の削除: " + "、".join(result.failures()[:3]))
    if d.object_id is not None:
        result = client.apply_edits(tables.exercises, deletes=[d.object_id], token=token)
        if result.failures():
            raise EsriError(code="E305", detail="問題の行の削除: " + "、".join(result.failures()[:3]))


# ---- answers from observation records ---------------------------------------------------------------------------

def answer_from_record(o: Any, exercise_type: str, before_things: Optional[set] = None) -> Optional[DraftAnswer]:
    """
    An answer of an observation record (core/obs's Observation: lon, lat, kind, activity, length, width, heading,
    present, unseen, object_id). 変化を見つける: 消失 when the record says so, 継続 when the thing was recorded on the
    image before (`before_things`: their object ids), else 来た. None for a record not usable (不可, or 消失 for another
    kind of exercise).
    """
    if getattr(o, "unseen", "") or getattr(o, "deleted_at", None) is not None:
        return None
    present = bool(getattr(o, "present", True))
    change = ""
    if exercise_type == CHANGE:
        change = GONE if not present else STAYED if before_things and o.object_id in before_things else CAME
    elif not present:
        return None
    # (a length measured makes the place's tolerance as big as the thing: scoring.toleranceByLength)
    return DraftAnswer(float(o.lon), float(o.lat), kind=o.kind or "", activity="" if change == GONE else (o.activity or ""),
                       length=o.length, width=o.width, heading=o.heading, change=change, explanation=(getattr(o, "note", "") or "").strip())


def answers_from_records(records: Iterable[Any], exercise_type: str, image_id: str, before_id: str = "",
                         area: Sequence = ()) -> list[tuple[Any, DraftAnswer]]:
    """
    The records of an image (`image_id`, as the observation dock names it) that can be answers, in the area (all of
    the image without one), each with its answer: (record, answer), the nearest the area's top left first.
    """
    records = [o for o in records if getattr(o, "deleted_at", None) is None]
    before = {o.object_id for o in records if before_id and o.image_id == before_id and o.present and not o.unseen}
    out = []
    for o in records:
        if o.image_id != image_id:
            continue
        if area and not inside(o.lon, o.lat, area):
            continue
        a = answer_from_record(o, exercise_type, before)
        if a is not None:
            out.append((o, a))
    return sorted(out, key=lambda p: (-round(p[1].lat, 4), p[1].lon))
