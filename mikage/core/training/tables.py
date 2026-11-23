"""
判読の練習's Esri tables (練習モードの段階 3 の 2): the organisation's exercises
and their answers, read while practising, and the results, one row per try
submitted (the one thing the practice writes to the production).

* TBL_TRAINING_EXERCISE (polygon: the area): one exercise;
* TBL_TRAINING_ANSWER (point): one thing of an exercise as it truly is;
* TBL_TRAINING_RESULT (table): one try.

The settings file's `training.exercises` says where they are:
`{"exercises": "<url>", "answers": "<url>", "results": "<url>", "token": "…"}`
(`results` may be left out: the tries are then kept on the computer only).
Their columns (`EXERCISE_FIELDS`, `ANSWER_FIELDS`, `RESULT_FIELDS`) are read
whatever their case. Pure Python: the rows are Esri JSON features (geometry in
WGS 84) as EsriClient.query_all gives them.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Optional

from .exercises import (
    Answer, CHANGE, CHANGE_WORDS, CHANGES, DEFAULT_SCORING, EXAM, Exercise, MODE_WORDS, PRACTICE, Scoring, scoring_of, TYPE_WORDS, TYPES,
)

#: An organisation's exercises' source (their keys: 組織:<EXERCISE_ID>).
SOURCE = "組織"
#: The other sources whose tries the results table keeps (their EXERCISE_ID: the key, 見本:S1).
OTHER_SOURCES = ("見本",)
TABLE_KEYS = ("exercises", "answers", "results", "token")
#: The columns, with what each holds (tools/arcgis/mikage_schema.py makes the tables).
EXERCISE_FIELDS = {
    "EXERCISE_ID": "問題の ID（答えの表と結果の表がこれで結ぶ）",
    "TITLE": "題名", "INSTRUCTION": "指示文", "EXERCISE_TYPE": "種類（見つける・見分ける・測る・変化を見つける）",
    "IMAGE_ID": "画像 ID（画像カタログ）", "IMAGE_SOURCE": "画像のファイルか URL（画像 ID がないとき）",
    "BEFORE_IMAGE_ID": "前の画像の ID（変化を見つける）", "BEFORE_IMAGE_SOURCE": "前の画像のファイルか URL",
    "TARGET_KINDS": "対象の種別（; 区切り。例: 船舶）", "DIFFICULTY": "難しさ（易・中・難など）",
    "TIME_LIMIT_MIN": "制限時間（分）", "EXERCISE_MODE": "練習 か 試験", "SHOW_ANSWERS": "答えと解説を見せる（1・0）",
    "PASS_SCORE": "合格点", "SCORING": "採点の決まり（JSON。training.scoring と同じ書き方で、その一部）",
    "COURSE_NAME": "コース", "COURSE_ORDER": "コースの中の順番", "ROLES": "対象の担当（, 区切りの符号。空: だれでも）",
    "ACTIVE": "出す（1）か出さない（0: 作りかけ）",
}
ANSWER_FIELDS = {
    "EXERCISE_ID": "問題の ID", "ANSWER_NO": "番号（空: 表の順）", "KIND": "種別（分類体系の > 区切り）", "ACTIVITY": "活動",
    "LENGTH_M": "長さ（m）", "WIDTH_M": "幅（m）", "HEADING": "向き（度。北から時計回り、後ろから前）",
    "CHANGE_KIND": "変化（来た・継続・消失）", "ANSWER_POINTS": "配点（空: 10）", "TOLERANCE_M": "位置の許容（m。空: 採点の決まり）",
    "REQUIRED": "必ず見つけるもの（1）か加点（0）", "EXPLANATION": "解説", "REFERENCE_IMAGE": "参考写真のファイルか URL",
}
RESULT_FIELDS = {
    "EXERCISE_ID": "問題の ID", "TITLE": "題名", "COURSE_NAME": "コース", "USER_NAME": "解いた人", "EXERCISE_MODE": "練習 か 試験",
    "ATTEMPT": "何回目", "STARTED": "始めた日時", "FINISHED": "提出した日時", "SECONDS": "かかった秒数", "SCORE": "点（0〜100）",
    "PASSED": "合格（1・0）", "FOUND": "見つけた数", "MISSED": "見落とした数", "EXTRA": "誤りの数", "KIND_SCORE": "種別の正しさ（%）",
    "HINTS": "ヒントを見た回数", "DETAIL": "答えごとの結果（JSON）",
}
#: DETAIL's length (a text column; longer is cut, the counts kept).
DETAIL_LENGTH = 30000
_TRUE = {"1", "true", "yes", "y", "on", "はい", "公開", "出す", "必須", "見せる"}
_FALSE = {"0", "false", "no", "n", "off", "いいえ", "非公開", "下書き", "作りかけ", "出さない", "加点", "見せない"}
_LIST = re.compile(r"\s*[;；,、]\s*")


@dataclass(frozen=True)
class ExerciseTables:
    """Where the organisation's exercises are (the settings file's `training.exercises`)."""
    exercises: str
    answers: str
    #: "" : the tries kept on the computer only.
    results: str = ""
    token: str = ""


def exercise_tables_of(value: Any, problems: list[str]) -> Optional[ExerciseTables]:
    """`training.exercises` (None when absent or not usable)."""
    if value is None or value == {} or value == "":
        return None
    if not isinstance(value, dict):
        problems.append('training.exercises は {"exercises": "URL", "answers": "URL", "results": "URL"} の形で指定')
        return None
    unknown = [k for k in value if k not in TABLE_KEYS]
    if unknown:
        problems.append(f"training.exercises に不明なキー「{'・'.join(unknown)}」（{'・'.join(TABLE_KEYS)}）")
    urls = {}
    for key in ("exercises", "answers", "results"):
        v = value.get(key, "")
        if not isinstance(v, str):
            problems.append(f"training.exercises.{key} は表の URL で指定")
            v = ""
        urls[key] = v.strip().rstrip("/")
    token = value.get("token", "")
    if not isinstance(token, str):
        problems.append("training.exercises.token は文字列で指定")
        token = ""
    if not urls["exercises"] or not urls["answers"]:
        problems.append("training.exercises には問題の表（exercises）と答えの表（answers）の URL が必要")
        return None
    return ExerciseTables(urls["exercises"], urls["answers"], urls["results"], token)


# ---- reading the rows ------------------------------------------------------------------------------------

def _value(attributes: dict, name: str) -> Any:
    """A column's value whatever the case of its name (None when there is no such column or it is empty)."""
    if name in attributes:
        v = attributes[name]
    else:
        low = name.lower()
        v = next((x for k, x in attributes.items() if k.lower() == low), None)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _text(attributes: dict, name: str) -> str:
    v = _value(attributes, name)
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def _number(attributes: dict, name: str, problems: list[str], where: str) -> Optional[float]:
    v = _value(attributes, name)
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        problems.append(f"{where}: {name} が数ではない（{v}）")
        return None
    if not math.isfinite(n):
        problems.append(f"{where}: {name} が数ではない（{v}）")
        return None
    return n


def _flag(attributes: dict, name: str, default: bool, problems: list[str], where: str) -> bool:
    v = _value(attributes, name)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    word = str(int(v) if isinstance(v, float) and v.is_integer() else v).strip().lower()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    problems.append(f"{where}: {name} は 1 か 0 で指定（{v}）")
    return default


def _words(attributes: dict, name: str) -> list[str]:
    v = _value(attributes, name)
    if v is None:
        return []
    if isinstance(v, str) and v.startswith("["):
        try:
            listed = json.loads(v)
        except ValueError:
            listed = None
        if isinstance(listed, list):
            return [str(x).strip() for x in listed if str(x).strip()]
    return [w for w in _LIST.split(str(v)) if w]


def _rings(geometry: Any) -> list:
    """A polygon's rings (longitude / latitude), [] when it has none."""
    if not isinstance(geometry, dict):
        return []
    out = []
    for ring in geometry.get("rings") or []:
        points = [(float(p[0]), float(p[1])) for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(points) >= 3:
            out.append(points)
    return out


def _point(geometry: Any) -> Optional[tuple]:
    if not isinstance(geometry, dict):
        return None
    try:
        x, y = float(geometry["x"]), float(geometry["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def exercise_of(feature: dict, base: Scoring, problems: list[str]) -> Optional[Exercise]:
    """An exercise of a TBL_TRAINING_EXERCISE row (without its answers; None: not usable, or not ACTIVE)."""
    a = feature.get("attributes") or {}
    eid = _text(a, "EXERCISE_ID")
    if not eid:
        problems.append("問題の表: EXERCISE_ID が空の行あり（読まない）")
        return None
    where = f"問題 {eid}"
    if not _flag(a, "ACTIVE", True, problems, where):
        return None
    words = _text(a, "EXERCISE_TYPE")
    kind = words if words in TYPES else TYPE_WORDS.get(words.lower(), "")
    if not kind:
        problems.append(f"{where}: EXERCISE_TYPE は {'・'.join(TYPES)} のどれか（{words or '空'}）")
        return None
    image_id, source = _text(a, "IMAGE_ID"), _text(a, "IMAGE_SOURCE")
    if not image_id and not source:
        problems.append(f"{where}: 画像なし（IMAGE_ID か IMAGE_SOURCE）")
        return None
    before_id, before_source = _text(a, "BEFORE_IMAGE_ID"), _text(a, "BEFORE_IMAGE_SOURCE")
    if kind == CHANGE and not before_id and not before_source:
        problems.append(f"{where}: 変化を見つける問題に前の画像なし（BEFORE_IMAGE_ID か BEFORE_IMAGE_SOURCE）")
        return None
    mode_words = _text(a, "EXERCISE_MODE")
    mode = mode_words if mode_words in (PRACTICE, EXAM) else MODE_WORDS.get(mode_words.lower(), PRACTICE if not mode_words else "")
    if not mode:
        problems.append(f"{where}: EXERCISE_MODE は 練習 か 試験（{mode_words}）。練習として出題")
        mode = PRACTICE
    scoring = base
    text = _text(a, "SCORING")
    if text:
        try:
            value = json.loads(text)
        except ValueError:
            problems.append(f"{where}: SCORING が JSON ではない")
            value = None
        scoring = scoring_of(value, problems, at=f"{where} の SCORING", base=base)
    pass_score = _number(a, "PASS_SCORE", problems, where)
    if pass_score is not None:
        if 0 <= pass_score <= 100:
            scoring = replace(scoring, pass_score=pass_score)
        else:
            problems.append(f"{where}: PASS_SCORE は 0〜100（{pass_score:g}）")
    limit = _number(a, "TIME_LIMIT_MIN", problems, where)
    order = _number(a, "COURSE_ORDER", problems, where)
    return Exercise(
        eid, _text(a, "TITLE") or eid, kind, image_id, _rings(feature.get("geometry")), instruction=_text(a, "INSTRUCTION"),
        image_source=source if not image_id else "", before_image_id=before_id, before_image_source=before_source if not before_id else "",
        target_kinds=_words(a, "TARGET_KINDS"), difficulty=_text(a, "DIFFICULTY"),
        time_limit=limit if limit is not None and limit > 0 else None, mode=mode,
        show_answers=_flag(a, "SHOW_ANSWERS", True, problems, where), scoring=scoring, course=_text(a, "COURSE_NAME"),
        order=int(order) if order is not None else 0, roles=_words(a, "ROLES"), source=SOURCE)


def answer_of(feature: dict, problems: list[str]) -> Optional[tuple[str, Answer]]:
    """(its exercise's id, the answer) of a TBL_TRAINING_ANSWER row; None when not usable."""
    a = feature.get("attributes") or {}
    eid = _text(a, "EXERCISE_ID")
    at = _point(feature.get("geometry"))
    if not eid or at is None:
        problems.append("答えの表: EXERCISE_ID か位置のない行あり（読まない）")
        return None
    where = f"問題 {eid} の答え"
    change_words = _text(a, "CHANGE_KIND")
    change = change_words if change_words in CHANGES else CHANGE_WORDS.get(change_words.lower(), "")
    if change_words and not change:
        problems.append(f"{where}: CHANGE_KIND は {'・'.join(CHANGES)} のどれか（{change_words}）")
    points = _number(a, "ANSWER_POINTS", problems, where)
    number = _number(a, "ANSWER_NO", problems, where)
    tolerance = _number(a, "TOLERANCE_M", problems, where)
    return eid, Answer(
        at[0], at[1], kind=_text(a, "KIND"), activity=_text(a, "ACTIVITY"), length=_number(a, "LENGTH_M", problems, where),
        width=_number(a, "WIDTH_M", problems, where), heading=_number(a, "HEADING", problems, where), change=change,
        points=points if points is not None and points > 0 else 10.0, tolerance=tolerance if tolerance is not None and tolerance > 0 else None,
        required=_flag(a, "REQUIRED", True, problems, where), explanation=_text(a, "EXPLANATION"), reference=_text(a, "REFERENCE_IMAGE"),
        number=int(number) if number is not None and number > 0 else 0)


def org_exercises(exercise_rows: Iterable[dict], answer_rows: Iterable[dict], base: Scoring = DEFAULT_SCORING,
                  problems: Optional[list[str]] = None) -> list[Exercise]:
    """
    The organisation's exercises (ACTIVE, with their answers numbered), by course (in the order the rows have
    them) and their order in it.
    """
    problems = problems if problems is not None else []
    answers: dict[str, list[Answer]] = {}
    for row in answer_rows:
        got = answer_of(row, problems)
        if got is not None:
            answers.setdefault(got[0], []).append(got[1])
    out, seen = [], set()
    for row in exercise_rows:
        e = exercise_of(row, base, problems)
        if e is None:
            continue
        if e.id in seen:
            problems.append(f"問題 {e.id}: EXERCISE_ID が重複（初めの行だけ読む）")
            continue
        seen.add(e.id)
        mine = sorted(answers.get(e.id, []), key=lambda x: (x.number <= 0, x.number))
        if not mine:
            problems.append(f"問題 {e.id}: 答えの表に答えなし（出題しない）")
            continue
        e.answers = mine
        out.append(e.numbered())
    # (the courses in the order the table has them first: as the instructor entered them)
    courses = {c: i for i, c in enumerate(dict.fromkeys(e.course for e in out))}
    return sorted(out, key=lambda e: (courses[e.course], e.order, e.title))


# ---- the results ------------------------------------------------------------------------------------------

def _ms(iso: str) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def result_attributes(record: dict) -> dict:
    """A try (results.py's result_record) as a TBL_TRAINING_RESULT row's values."""
    detail = json.dumps({"parts": record.get("parts") or {}, "penalty": record.get("penalty") or 0, "detail": record.get("detail") or []},
                        ensure_ascii=False, separators=(",", ":"))
    if len(detail) > DETAIL_LENGTH:
        detail = json.dumps({"parts": record.get("parts") or {}, "penalty": record.get("penalty") or 0, "cut": True},
                            ensure_ascii=False, separators=(",", ":"))
    kind = record.get("kindScore")
    # (an exercise not the organisation's, the sample's: its key, 見本:S1, so it is not taken for an organisation's)
    source = record.get("source") or SOURCE
    eid = (record.get("exerciseId") or "") if source == SOURCE else (record.get("exercise") or "")
    return {
        "EXERCISE_ID": eid, "TITLE": (record.get("title") or "")[:255], "COURSE_NAME": (record.get("course") or "")[:255],
        "USER_NAME": record.get("user") or "", "EXERCISE_MODE": record.get("mode") or PRACTICE, "ATTEMPT": int(record.get("attempt") or 1),
        "STARTED": _ms(record.get("started") or ""), "FINISHED": _ms(record.get("finished") or ""), "SECONDS": int(record.get("seconds") or 0),
        "SCORE": float(record.get("score") or 0), "PASSED": 1 if record.get("passed") else 0, "FOUND": int(record.get("found") or 0),
        "MISSED": int(record.get("missed") or 0), "EXTRA": int(record.get("extra") or 0), "KIND_SCORE": kind,
        "HINTS": int(record.get("hints") or 0), "DETAIL": detail,
    }


def record_of(feature: dict) -> Optional[dict]:
    """A TBL_TRAINING_RESULT row as a try (the keys results.py's lists and best_of read), None when not one."""
    a = feature.get("attributes") or {}
    eid = _text(a, "EXERCISE_ID")
    if not eid:
        return None
    problems: list[str] = []

    def iso(name: str) -> str:
        ms = _number(a, name, problems, "")
        return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds") if ms is not None else ""

    score = _number(a, "SCORE", problems, "")
    source, key = SOURCE, f"{SOURCE}:{eid}"
    other, _colon, rest = eid.partition(":")
    if other in OTHER_SOURCES and rest:
        source, key, eid = other, eid, rest
    out = {
        "exercise": key, "exerciseId": eid, "source": source, "title": _text(a, "TITLE"), "course": _text(a, "COURSE_NAME"),
        "mode": _text(a, "EXERCISE_MODE") or PRACTICE, "attempt": int(_number(a, "ATTEMPT", problems, "") or 1), "user": _text(a, "USER_NAME"),
        "started": iso("STARTED"), "finished": iso("FINISHED"), "seconds": int(_number(a, "SECONDS", problems, "") or 0),
        "score": score if score is not None else 0, "passed": _flag(a, "PASSED", False, problems, ""),
        "found": int(_number(a, "FOUND", problems, "") or 0), "missed": int(_number(a, "MISSED", problems, "") or 0),
        "extra": int(_number(a, "EXTRA", problems, "") or 0), "kindScore": _number(a, "KIND_SCORE", problems, ""),
        "hints": int(_number(a, "HINTS", problems, "") or 0), "fromTable": True,
    }
    detail = _text(a, "DETAIL")
    if detail:
        # (read for the instructors' 取り違え: the person's own tries are read without it)
        try:
            value = json.loads(detail)
        except ValueError:
            value = None
        if isinstance(value, dict):
            out.update(parts=value.get("parts") or {}, penalty=value.get("penalty") or 0, detail=value.get("detail") or [])
    return out


def quoted(text: str) -> str:
    """A text in an SQL where clause."""
    return "'" + text.replace("'", "''") + "'"


def user_where(user: str) -> str:
    """The where clause of a person's rows in the results table."""
    return f"USER_NAME = {quoted(user)}"


# ---- reading and writing (in a task: `client` is an EsriClient) ---------------------------------------------

def read_exercises(client: Any, tables: ExerciseTables, token: Optional[str], base: Scoring = DEFAULT_SCORING,
                   problems: Optional[list[str]] = None, cancelled=None) -> list[Exercise]:
    """The organisation's exercises with their answers, read from its tables."""
    exercises = client.query_all(tables.exercises, "1=1", token, cancelled=cancelled, limit=20_000)
    answers = client.query_all(tables.answers, "1=1", token, cancelled=cancelled, limit=200_000)
    return org_exercises(exercises, answers, base, problems)


def read_results_of(client: Any, tables: ExerciseTables, user: str, token: Optional[str], cancelled=None) -> list[dict]:
    """A person's tries kept in the results table (another computer's too); [] without the table or a name."""
    if not tables.results or not user:
        return []
    rows = client.query_all(tables.results, user_where(user), token, geometry=False, cancelled=cancelled, limit=100_000,
                            out_fields=",".join(f for f in RESULT_FIELDS if f != "DETAIL"))
    return [r for r in (record_of(row) for row in rows) if r is not None]


def send_results(client: Any, tables: ExerciseTables, records: list[dict], token: Optional[str]) -> tuple[list[bool], list[str]]:
    """
    Tries added to the results table, one row each (the columns it has): whether each went in (all False without
    the table), and why those that did not failed.
    """
    if not tables.results or not records:
        return [False] * len(records), []
    # (only the columns the table has, by its own names: a table made without one of them still takes the rest)
    names = {n.lower(): n for n in client.layer(tables.results, token).field_types}
    adds = [{"attributes": {names[k.lower()]: v for k, v in result_attributes(r).items() if k.lower() in names}} for r in records]
    results = client.apply_edits(tables.results, adds=adds, token=token)
    adds = results.add_results
    return [bool(i < len(adds) and adds[i].get("success")) for i in range(len(records))], results.failures()
