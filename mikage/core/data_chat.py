"""
データに質問 (tsudo 2026-11-24: 「ミカゲと接続してる各データを参照した生成AIと
対話できる機能を検討して」 → design doc 「ミカゲのデータと対話する生成 AI」,
then 「両方に対応（まず PC の 9B）」): a question on the settings file's
tables (「先月の舞鶴の記録は何件？」) answered from what they hold.

The generative model never sees the rows it is asked about until code has
read and counted them; it writes two things only:

1. one SQLite SELECT over the tables (the 「データ分析」 engine, core/analysis.py
   and core/analysis_plan.py, then reads only what the SELECT needs from the
   Esri tables and runs it read-only);
2. 1–3 sentences on the result, which code checks against the table
   (numbers, name–number pairs, order, quoted words and ids): a sentence
   that does not match is replaced by one code writes.

What the model is shown is made easier than the tables are (measured on 30
questions: Qwen3.5-9B 24 → 26 right): records deleted (DELETED_AT set) are
left out by the table's read condition, and the record's place is a column
of its own (PLACE_NAME: code joins TBL_NAMED_AREA). Dates the questions name
(今日, 先週, 先月 …) are worked out by code and given to the model, and a
result's months and days are labelled for the answer (2026-10 → 「先月」).

This module is pure Python: the prompt, the SQL's check and rewrite, the
answer's check. The tables are read in qgis_io/data_chat_io.py.
"""
from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from . import analysis_plan
from .analysis import KINDS, Table, fold
from .text_assist import Message, TextTask

TASK = "dataChat"

#: The tables offered (when the settings name none), by kind: those measured in the study (2026-11-24).
SHOWN_KINDS = ("observation", "namedArea", "catalog", "aiResult", "alertLog", "request", "requestHistory", "event", "eventLink",
               "assignment", "handover")
#: The record's place, a column code adds to 観測記録 (TBL_NAMED_AREA joined by NAMED_AREA_OBJECTID).
PLACE_COLUMN = "PLACE_NAME"
#: At most this many of a column's values are named to the model (more: these and 「など」).
VALUES_LIMIT = 15
#: At most this many rows of a result are shown to the model for its answer.
ANSWER_ROWS = 20
#: At most this many earlier questions are kept for a follow-up (「では先々月は？」).
HISTORY = 3
#: How long the answer may be (tokens).
ANSWER_TOKENS = 240
WEEKDAYS = "月火水木金土日"


@dataclass(frozen=True)
class Col:
    """A column the model is told of: its role in the plugin and what it holds."""
    role: str
    note: str
    #: Its values, when they are a fixed list (docs/admin/esri-tables.md).
    values: tuple[str, ...] = ()
    #: Its values are read from the table (the distinct ones), when not too many.
    read: bool = False
    #: A path of classes (「船舶 > 艦艇 > 護衛艦」): examples and the LIKE for a top class.
    path: bool = False


#: Each kind's line and columns, in the order they are told (the study's schema, docs/admin/esri-tables.md).
NOTES: dict[str, tuple[str, tuple[Col, ...]]] = {
    "observation": ("観測記録（1 行 = 画像の上の 1 つのモノの記録。削除済みは入っていない）", (
        Col("id", "記録ID"), Col("imageId", "画像ID"), Col("imageTime", "撮像日時"), Col("objectId", "モノID"),
        Col("objectName", "モノの名前（艦名など。空が多い）"), Col("kind", "種別のパス", read=True, path=True),
        Col("activity", "活動", read=True), Col("present", "在不在（1=在 0=消失 NULL=見えなかった）"),
        Col("note", "備考（AI の結果から取り込んだ記録は '[AI:' を含む）"), Col("length", "長さ(m)"), Col("heading", "向き(度)"),
        Col("satellite", "衛星", read=True), Col("confidence", "確度", read=True), Col("recordedBy", "記録者", read=True),
        Col("created", "記録した日時"), Col("unseen", "見えなかった理由"))),
    "namedArea": ("地名", (
        Col("name", "地名", read=True), Col("kind", "種類", read=True), Col("active", "有効（0 は使わない）"), Col("id", "管理番号"))),
    "catalog": ("画像カタログ（1 行 = 1 画像）", (
        Col("id", "画像ID"), Col("acquired", "撮像日時"), Col("registered", "登録日時"), Col("sensor", "センサー", read=True),
        Col("angle", "撮像の角度(度)"))),
    "aiResult": ("外部 AI の検出結果", (
        Col("id", "結果ID"), Col("imageId", "画像ID"), Col("class", "クラス", read=True), Col("score", "確信度(0〜1)"),
        Col("model", "モデル", read=True), Col("detectedAt", "検出日時"), Col("kind", "種別のパス", path=True))),
    "alertLog": ("通報記録（1 行 = 通報した 1 記録）", (
        Col("obsId", "観測記録の記録ID"), Col("report", "通報の種類", read=True), Col("text", "通報文"), Col("reportedAt", "通報日時"),
        Col("reportedBy", "通報者", read=True), Col("batch", "1 回の通報の ID"), Col("manual", "手動で足した記録は 1"))),
    "request": ("撮像依頼台帳", (
        Col("id", "依頼ID"), Col("name", "依頼の名前"), Col("managementNo", "管理番号"), Col("group", "依頼グループ（空はグループなし）", read=True),
        Col("category", "区分", ("定点", "臨時")), Col("achieve", "達成条件", ("毎回", "期間内1回")), Col("start", "開始"),
        Col("end", "終了"), Col("status", "状態", ("有効", "停止", "完了")), Col("priority", "優先度（数が小さいほど優先度が高い）"),
        Col("kind", "種別", ("任意", "光学", "SAR")), Col("requester", "依頼者", read=True))),
    "requestHistory": ("日ごとの撮像予定（1 行 = 1 依頼・1 日）", (
        Col("day", "対象日 'YYYY-MM-DD'"), Col("requestId", "依頼ID"), Col("name", "依頼の名前"), Col("system", "出力したシステム", read=True),
        Col("satellite", "衛星", read=True), Col("result", "結果（採用 = 撮像予定として出力した・見送り・要求外）"),
        Col("reason", "見送りの理由"), Col("captured", "撮像結果", ("撮像済み", "未撮像", "未判定")),
        Col("adoption", "システムの採否", ("採用", "不採用", "不明")), Col("adoptionReason", "不採用の理由"), Col("plannedAt", "撮像予定日時"),
        Col("imageId", "画像ID"))),
    "event": ("イベント", (
        Col("id", "イベントID"), Col("name", "名前"), Col("kind", "種類", read=True), Col("status", "状態", ("対応中", "終了")),
        Col("start", "開始"), Col("end", "終了"))),
    "eventLink": ("イベントの紐付け", (
        Col("event", "イベントID"), Col("type", "種類", ("image", "observation", "request")), Col("item", "画像ID・記録ID・依頼ID"),
        Col("time", "項目の日時"))),
    "assignment": ("画像の分担", (
        Col("imageId", "画像ID"), Col("imageTime", "撮像日時"), Col("assignee", "担当者", read=True),
        Col("status", "状態", ("作業中", "保留", "完了")), Col("takenAt", "担当にした日時"), Col("doneAt", "完了にした日時"))),
    "handover": ("引き継ぎメモ", (
        Col("text", "本文"), Col("memo", "申し送り"), Col("start", "期間開始"), Col("end", "期間終了"), Col("writtenBy", "書いた人"))),
}

#: The answer's instructions.
ANSWER_PROMPT = (
    "あなたは衛星画像の判読システム「ミカゲ」の案内役です。利用者の質問と、プログラムがデータベースから読んだ結果の表が渡されます。\n"
    "- 結果の表だけを使って、日本語で 1〜3 文で答える\n"
    "- 表にない数・名前・理由は書かない。推測や助言は書かない\n"
    "- 0 行なら「該当するものはありませんでした」と書く\n"
    "- 行が多いときは数と主なものだけ書く。「プログラムが数えた数」があればその数を使う\n"
    "- 表は並べた順のまま。順を逆にしない\n"
    "【ここまで】"
)


class ChatError(Exception):
    """A question that cannot be answered as written (the reason, for the person)."""


# ---- the tables told to the model --------------------------------------------------------------------

@dataclass
class Shown:
    """A table told to the model: its columns (real names) with what each holds."""
    table: Table
    title: str
    #: (column name, note) in the order told.
    columns: list[tuple[str, str]] = field(default_factory=list)
    #: The real names by role (of the columns it has).
    names: dict[str, str] = field(default_factory=dict)


@dataclass
class Schema:
    """What the model is told of the tables (the SQL system prompt's middle), and what code needs to rewrite its SQL."""
    shown: list[Shown]
    #: 観測記録 joined to 地名 by code: (observation table, its NAMED_AREA_OBJECTID, the named area table, its OBJECTID, its NAME).
    place: Optional[tuple[str, str, str, str, str]] = None
    #: The read condition of each table (records deleted left out): {table: Esri WHERE}.
    conditions: dict[str, str] = field(default_factory=dict)
    #: Labels of the columns (real name upper → label) for the answer's table.
    labels: dict[str, str] = field(default_factory=dict)
    #: The columns 観測記録 and 地名 both have (folded: KIND, ACTIVE …): qualified when code joins them.
    shared: set[str] = field(default_factory=set)

    @property
    def names(self) -> list[str]:
        return [s.table.name for s in self.shown]

    def of(self, kind: str) -> Optional[Shown]:
        return next((s for s in self.shown if s.table.kind == kind), None)

    def column(self, kind: str, role: str) -> Optional[str]:
        s = self.of(kind)
        return s.names.get(role) if s is not None else None


def offered_tables(tables: Sequence[Table], wanted: Sequence[str] = ()) -> list[Table]:
    """The Esri tables told to the model: those `wanted` names (SQL names or kinds), else those of SHOWN_KINDS, in their order."""
    esri = [t for t in tables if t.source == "esri"]
    if wanted:
        w = {fold(x) for x in wanted}
        return [t for t in esri if fold(t.name) in w or fold(t.kind) in w]
    return [t for t in esri if t.kind in SHOWN_KINDS]


def _values_note(values: Sequence[str], col: Col, name: str) -> str:
    if col.path:
        tops = list(dict.fromkeys(v.split(">")[0].strip() for v in values if v.strip()))
        examples = sorted(values, key=lambda v: (-v.count(">"), v))
        picked: list[str] = []
        for top in tops:
            picked += [v for v in examples if v.startswith(top)][:2]
        picked = list(dict.fromkeys(picked))[:8]
        said = " ".join(f"'{v}'" for v in picked)
        like = f"。大分類は {name} LIKE '{tops[0]}%'" if tops else ""
        return f"（例 {said}{like}）" if said else ""
    if not values:
        return ""
    more = "など" if len(values) > VALUES_LIMIT else ""
    return "（" + "・".join(values[:VALUES_LIMIT]) + more + "）"


def schema_of(tables: Sequence[Table], values: Optional[dict[tuple[str, str], list[str]]] = None) -> Schema:
    """
    What the model is told of `tables` (their columns known: Table.columns,
    else the roles' names): the columns of NOTES that each has, with their
    values (fixed, or `values[(table, column)]` read from it), 観測記録's
    PLACE_NAME when the named areas are among them and the records have
    their key, and each table's read condition (records deleted left out).
    """
    values = values or {}
    shown: list[Shown] = []
    labels: dict[str, str] = {}
    for t in tables:
        title, cols = NOTES.get(t.kind, (KINDS.get(t.kind, ("", t.title))[1] or t.title, ()))
        have = {c.name.upper(): c.name for c in t.columns} if t.columns else None
        s = Shown(t, title)
        for col in cols:
            name = t.roles.get(col.role)
            if not name or (have is not None and name.upper() not in have):
                continue
            name = have[name.upper()] if have is not None else name
            s.names[col.role] = name
            listed = list(col.values) or list(values.get((t.name, name), []))
            note = col.note + (_values_note(listed, col, name) if listed else "")
            s.columns.append((name, note))
            labels.setdefault(name.upper(), re.split(r"[（(]", col.note)[0].strip())
        if not cols and t.columns:
            # A table without notes: its columns by their aliases.
            for c in t.columns:
                if c.name.upper() == "SHAPE" or c.oid:
                    continue
                s.columns.append((c.name, c.alias if c.alias and c.alias != c.name else ""))
                s.names.setdefault(c.role or c.name, c.name)
        # Its object id (OBJECTID), for the named areas' key.
        oid = next((c.name for c in t.columns if c.oid), None) if t.columns else None
        if oid:
            s.names.setdefault("@objectId", oid)
        shown.append(s)
    schema = Schema(shown, labels=labels)
    obs, area = schema.of("observation"), schema.of("namedArea")
    deleted = obs.table.roles.get("deleted") if obs is not None else None
    if obs is not None and deleted and (not obs.table.columns or any(c.name.upper() == deleted.upper() for c in obs.table.columns)):
        schema.conditions[obs.table.name] = f"{deleted} IS NULL"
    key = obs.table.roles.get("namedAreaObjectId") if obs is not None else None
    has_key = obs is not None and key and (not obs.table.columns or any(c.name.upper() == key.upper() for c in obs.table.columns))
    if obs is not None and area is not None and has_key and area.names.get("name"):
        place_values = values.get((area.table.name, area.names["name"]), [])
        note = "記録の地名" + _values_note(place_values, Col("name", ""), PLACE_COLUMN)
        at = next((i for i, (n, _x) in enumerate(obs.columns) if n.upper() == (obs.names.get("created") or "").upper()), len(obs.columns))
        obs.columns.insert(at + 1, (PLACE_COLUMN, note))
        schema.place = (obs.table.name, key, area.table.name, area.names.get("@objectId", "OBJECTID"), area.names["name"])
        labels[PLACE_COLUMN] = "地名"

        def names_of(t: Table) -> set[str]:
            return {fold(c.name) for c in t.columns} if t.columns else {fold(v) for v in t.roles.values()} | {"objectid"}

        schema.shared = names_of(obs.table) & names_of(area.table)
    return schema


def schema_text(schema: Schema) -> str:
    """The tables and their columns, as the model is told them."""
    lines = ["表と列（SQLite。日時はすべて日本時間の文字 'YYYY-MM-DD HH:MM:SS'。日だけの列は 'YYYY-MM-DD'）"]
    for s in schema.shown:
        lines.append(f"■ {s.table.name} {s.title}")
        line = ""
        for name, note in s.columns:
            item = f"{name} {note}".strip()
            if line and len(line) + len(item) > 110:
                lines.append("  " + line)
                line = ""
            line = f"{line} / {item}" if line else item
        if line:
            lines.append("  " + line)
    return "\n".join(lines)


def rules_text(schema: Schema, more: Sequence[str] = ()) -> str:
    """The rules, with the tables' own names."""
    out = ["決まり:"]
    image_time, created = schema.column("observation", "imageTime"), schema.column("observation", "created")
    if image_time and created:
        out.append(f"- 「いつの記録」「〜に撮った」は撮像日時 {image_time} で絞る。「記録した」「登録した」は {created}")
    if schema.place is not None:
        out.append(f"- 記録の地名は {schema.place[0]} の {PLACE_COLUMN}（結合しなくてよい）")
    history, result = schema.of("requestHistory"), schema.column("requestHistory", "result")
    if history is not None and result:
        out.append(f"- 「撮像予定」は {history.table.name} の {result} = '採用' の行")
    present = schema.column("observation", "present")
    if present:
        out.append(f"- 「最後に確認した」は {present} = 1 の記録")
    out += [f"- {r}" for r in more]
    out.append("- 読み取りだけ。表を変える頼み（停止にする・消すなど）や、表にない情報（天気など）は SQL を書かず「NG: 理由」と 1 行で書く")
    out.append("- 答えは SQL だけ（説明は書かない）")
    return "\n".join(out)


@dataclass(frozen=True)
class Periods:
    """The days the questions' words name (Japan's days), worked out by code."""
    now: datetime

    @property
    def today(self) -> date:
        return self.now.date()

    def named(self) -> list[tuple[str, date, date]]:
        """(word, first day, last day) of 今日 … 今年."""
        d = self.today
        week = d - timedelta(days=d.weekday())
        month = d.replace(day=1)
        next_month = (month + timedelta(days=32)).replace(day=1)
        last_month = (month - timedelta(days=1)).replace(day=1)
        return [("今日", d, d), ("昨日", d - timedelta(days=1), d - timedelta(days=1)), ("明日", d + timedelta(days=1), d + timedelta(days=1)),
                ("今週", week, week + timedelta(days=6)), ("先週", week - timedelta(days=7), week - timedelta(days=1)),
                ("今月", month, next_month - timedelta(days=1)), ("先月", last_month, month - timedelta(days=1)),
                ("直近7日", d - timedelta(days=6), d), ("今年", d.replace(month=1, day=1), d.replace(month=12, day=31))]

    def text(self) -> str:
        # The day only (not the time): the instructions stay the same all day, and are read once (kept).
        head = f"日付（今日は {self.today.isoformat()}（{WEEKDAYS[self.today.weekday()]}））: "
        parts = []
        for word, a, b in self.named():
            parts.append(f"{word} {a.isoformat()}" if a == b else f"{word} {a.isoformat()}〜{b.isoformat()}")
        return head + " / ".join(parts)

    def label(self, value: Any) -> str:
        """「（今月）」「（先月）」「（今日）」「（昨日）」 for a month or day of a result; "" else."""
        if not isinstance(value, str):
            return ""
        d = self.today
        month = d.replace(day=1)
        last = (month - timedelta(days=1)).replace(day=1)
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return "（今月）" if value == month.strftime("%Y-%m") else "（先月）" if value == last.strftime("%Y-%m") else ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return "（今日）" if value == d.isoformat() else "（昨日）" if value == (d - timedelta(days=1)).isoformat() else ""
        return ""


def system_text(task: TextTask, schema: Schema, periods: Periods) -> str:
    """The SQL conversation's instructions (the same for every question of the day: kept read, GenAiWriter keep)."""
    return f"{task.prompt}\n{schema_text(schema)}\n\n{rules_text(schema, task.rules)}\n\n{periods.text()}\n【ここまで】"


def examples_of(schema: Schema, periods: Periods, values: Optional[dict[tuple[str, str], list[str]]] = None) -> list[tuple[str, str]]:
    """The examples shown before the question, made with the tables' own names (the study's three)."""
    values = values or {}
    out: list[tuple[str, str]] = []
    named = {w: (a, b) for w, a, b in periods.named()}
    last_a, last_b = named["先月"]
    month_a = named["今月"][0]
    obs = schema.of("observation")
    if obs is not None and obs.names.get("imageTime"):
        t, when = obs.table.name, obs.names["imageTime"]
        where = [f"{when} >= '{last_a.isoformat()}'", f"{when} < '{month_a.isoformat()}'"]
        ask = "先月の記録"
        if schema.place is not None:
            places = _first(values.get((schema.place[2], schema.place[4])), "舞鶴")
            where.insert(0, f"{PLACE_COLUMN} = '{places[0]}'")
            ask = f"先月の{places[0]}の記録"
        conf = obs.names.get("confidence")
        if conf:
            level = _first(values.get((t, conf)), "確定")[0]
            where.insert(1 if schema.place is not None else 0, f"{conf} = '{level}'")
            ask += f"で、確度が{level}のもの"
        out.append((ask + "は何件？", f"SELECT COUNT(*) FROM {t} WHERE " + " AND ".join(where)))
    log = schema.of("alertLog")
    cat = schema.of("catalog")
    week = named["今週"][0].isoformat()
    if log is not None and log.names.get("reportedAt") and log.names.get("text"):
        at, text = log.names["reportedAt"], log.names["text"]
        out.append(("今週の通報を、通報日時と通報文で新しい順に",
                    f"SELECT {at}, {text} FROM {log.table.name} WHERE {at} >= '{week}' ORDER BY {at} DESC"))
    elif cat is not None and cat.names.get("acquired") and cat.names.get("id"):
        at, image = cat.names["acquired"], cat.names["id"]
        out.append(("今週撮った画像を、撮像日時と画像IDで新しい順に",
                    f"SELECT {at}, {image} FROM {cat.table.name} WHERE {at} >= '{week}' ORDER BY {at} DESC"))
    out.append(("来週の天気は？", "NG: 天気のデータはつながっていません"))
    return out


def _first(found: Optional[list[str]], usual: str) -> list[str]:
    """`found` with `usual` first when it is among them (else as found; [usual] when nothing was)."""
    if not found:
        return [usual]
    return [usual, *[v for v in found if v != usual]] if usual in found else list(found)


@dataclass(frozen=True)
class Turn:
    """An earlier question of the conversation and the SQL that answered it (for a follow-up)."""
    question: str
    sql: str


def sql_messages(task: TextTask, schema: Schema, periods: Periods, question: str, history: Sequence[Turn] = (),
                 values: Optional[dict[tuple[str, str], list[str]]] = None) -> list[Message]:
    """The SQL conversation: the instructions, the examples, the earlier questions (at most HISTORY) and the question."""
    m: list[Message] = [{"role": "system", "content": system_text(task, schema, periods)}]
    for q, a in (task.examples or tuple(examples_of(schema, periods, values))):
        m += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    for turn in list(history)[-HISTORY:]:
        m += [{"role": "user", "content": turn.question}, {"role": "assistant", "content": turn.sql}]
    if question:
        m.append({"role": "user", "content": question.strip()})
    return m


def retry_messages(messages: Sequence[Message], written: str, error: str) -> list[Message]:
    """The SQL conversation again, told the error of the SQL it wrote (one try more)."""
    return [*messages, {"role": "assistant", "content": written.strip()},
            {"role": "user", "content": f"この SQL はエラーになった: {error}\n直した SQL だけを書いてください（答えられないなら「NG: 理由」）。"}]


# ---- the SQL written --------------------------------------------------------------------------------

def sql_of(text: str) -> tuple[Optional[str], str]:
    """
    The SELECT the model wrote (its fences, explanations and what follows a ;
    left out), or (None, why) when it said it cannot (「NG: …」) or wrote none.
    """
    s = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    s = re.sub(r"^```(?:sql|sqlite)?\s*|```\s*$", "", s, flags=re.MULTILINE | re.IGNORECASE).strip()
    if re.match(r"(?i)^\s*NG\b", s):
        return None, re.sub(r"(?i)^\s*NG\s*[:：]?\s*", "", s).splitlines()[0].strip() if s else ""
    m = re.search(r"\b(SELECT|WITH)\b", s, re.IGNORECASE)
    if not m:
        return None, ""
    s = s[m.start():].split(";")[0]
    s = re.split(r"（|\n\s*\n|\n(?:この|説明|注意|※)", s)[0]
    return s.strip(), ""


_WRITES = {"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "ATTACH", "DETACH", "PRAGMA", "REPLACE", "VACUUM", "REINDEX"}


def check_sql(sql: str, names: Sequence[str]) -> None:
    """Raises ChatError when `sql` is not one SELECT over the tables `names` (and the names of its own WITH)."""
    toks = analysis_plan.tokens(sql)
    if not toks:
        raise ChatError("SQL を読めない（引用符が閉じていない）")
    if toks[0] not in (("kw", "SELECT"), ("kw", "WITH")):
        raise ChatError("SELECT 文ではない")
    if any(t == ("punct", ";") for t in toks[:-1]):
        raise ChatError("SQL が 2 つ以上ある")
    if any(k == "word" and v.upper() in _WRITES for k, v in toks):
        raise ChatError("表を変える SQL は実行しない（読み取りだけ）")
    known = {fold(n) for n in names}
    own = {fold(analysis_plan._name(*toks[i - 1])) for i, t in enumerate(toks) if i and t == ("kw", "AS")
           and i + 1 < len(toks) and toks[i + 1] == ("punct", "(") and toks[i - 1][0] in ("word", "qid")}
    unknown = []
    for i, t in enumerate(toks[:-1]):
        if t in (("kw", "FROM"), ("kw", "JOIN")) or (t == ("punct", ",") and _in_from(toks, i)):
            k, v = toks[i + 1]
            if k in ("word", "qid"):
                name = analysis_plan._name(k, v)
                if fold(name) not in known and fold(name) not in own and name not in unknown:
                    unknown.append(name)
    if unknown:
        raise ChatError(f"つながっていない表: {'・'.join(unknown)}")


def _in_from(toks: list, at: int) -> bool:
    """Whether the comma at `at` separates tables of a FROM (not columns of a SELECT)."""
    depth = 0
    for k, v in reversed(toks[:at]):
        if (k, v) == ("punct", ")"):
            depth += 1
        elif (k, v) == ("punct", "("):
            if depth == 0:
                return False
            depth -= 1
        elif depth == 0 and k == "kw" and v in ("FROM", "JOIN"):
            return True
        elif depth == 0 and k == "kw" and v in ("SELECT", "WHERE", "GROUP", "ORDER", "HAVING", "BY", "LIMIT"):
            return False
    return False


_CLAUSE_END = {"WHERE", "GROUP", "HAVING", "WINDOW", "ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT"}


@dataclass
class _Select:
    depth: int
    start: int
    #: Where its FROM ends (a token index): the place for a join code adds.
    from_end: Optional[int] = None
    in_from: bool = False
    select_end: Optional[int] = None
    #: Its WHERE (the token WHERE, and where it ends).
    where_start: Optional[int] = None
    where_end: Optional[int] = None
    #: (table name, alias) of its FROM.
    sources: list[tuple[str, str]] = field(default_factory=list)


def _selects(toks: list[tuple[str, str]]) -> tuple[list[_Select], list[int]]:
    """Each SELECT of the SQL (subqueries, UNION's parts) and, for each token, the SELECT it belongs to (-1: none)."""
    selects: list[_Select] = []
    owner: list[int] = []
    stack: list[int] = []
    depth = 0
    for i, t in enumerate(toks):
        if t == ("punct", "("):
            depth += 1
        if t == ("kw", "SELECT"):
            while stack and selects[stack[-1]].depth >= depth:
                s = selects[stack.pop()]
                if s.in_from and s.from_end is None:
                    s.from_end = i - 1 if i and toks[i - 1][0] == "kw" else i
                if s.where_start is not None and s.where_end is None:
                    s.where_end = i
            selects.append(_Select(depth, i))
            stack.append(len(selects) - 1)
        cur = selects[stack[-1]] if stack else None
        if cur is not None and depth == cur.depth:
            if cur.where_start is not None and cur.where_end is None and t[0] == "kw" and t[1] in _CLAUSE_END - {"WHERE"}:
                cur.where_end = i
            if t == ("kw", "WHERE") and cur.where_start is None:
                cur.where_start = i
            if t == ("kw", "FROM") and not cur.in_from and cur.from_end is None:
                cur.in_from, cur.select_end = True, i
            elif cur.in_from and cur.from_end is None and (t[0] == "kw" and t[1] in _CLAUSE_END):
                cur.from_end = i
            elif cur.in_from and cur.from_end is None and t[0] in ("word", "qid"):
                before = toks[i - 1]
                if before in (("kw", "FROM"), ("kw", "JOIN"), ("punct", ",")):
                    name = analysis_plan._name(*t)
                    alias = name
                    j = i + 1
                    if j < len(toks) and toks[j] == ("kw", "AS"):
                        j += 1
                    if j < len(toks) and toks[j][0] in ("word", "qid"):
                        alias = analysis_plan._name(*toks[j])
                    cur.sources.append((name, alias))
        owner.append(stack[-1] if stack else -1)
        if t == ("punct", ")"):
            depth -= 1
            while stack and selects[stack[-1]].depth > depth:
                s = selects[stack.pop()]
                if s.in_from and s.from_end is None:
                    s.from_end = i
                if s.where_start is not None and s.where_end is None:
                    s.where_end = i
    for s in selects:
        if s.in_from and s.from_end is None:
            s.from_end = len(toks)
        if s.where_start is not None and s.where_end is None:
            s.where_end = len(toks)
    return selects, owner


def _place_required(toks: list[tuple[str, str]], s: _Select) -> bool:
    """
    Whether the SELECT's WHERE keeps only records with a place (a part
    `PLACE_NAME = …`, IN, LIKE, GLOB joined by AND to the rest, no OR around
    it): the join is then an inner one, and the records are read for the
    places' keys (a question on one place reads that place's records only).
    """
    if s.where_start is None:
        return False
    body = toks[s.where_start + 1:s.where_end]
    parts: list[list[tuple[str, str]]] = [[]]
    depth, between = 0, False
    for t in body:
        if t == ("punct", "("):
            depth += 1
        elif t == ("punct", ")"):
            depth -= 1
        if depth == 0 and t == ("kw", "OR"):
            return False
        if depth == 0 and t == ("kw", "BETWEEN"):
            between = True
        if depth == 0 and t == ("kw", "AND"):
            if between:
                between = False
            else:
                parts.append([])
                continue
        parts[-1].append(t)
    for part in parts:
        if len(part) >= 3 and part[1] == ("punct", ".") and part[0][0] in ("word", "qid"):
            part = part[2:]
        if len(part) >= 2 and part[0][0] in ("word", "qid") and fold(analysis_plan._name(*part[0])) == fold(PLACE_COLUMN) \
                and (part[1] in (("op", "="), ("op", "=="), ("kw", "IN"), ("kw", "LIKE"), ("kw", "GLOB"))):
            return True
    return False


def expand_place(sql: str, schema: Schema) -> str:
    """
    `sql` with 観測記録's PLACE_NAME made real: each SELECT that reads the
    records and names it gets `LEFT JOIN TBL_NAMED_AREA _place ON
    o.NAMED_AREA_OBJECTID = _place.OBJECTID` (a JOIN when its WHERE keeps
    only records of a place: _place_required), PLACE_NAME being
    `_place.NAME`, and the columns both tables have (KIND) the records'. A
    plain SELECT stays one, so the 「データ分析」 engine still sends the
    conditions to the server (the place's to the named areas, and the
    records read for their keys). `sql` itself when it does not name PLACE_NAME.
    """
    if schema.place is None or fold(PLACE_COLUMN) not in fold(sql):
        return sql
    obs, key, area, oid, name = schema.place
    toks = analysis_plan.tokens(sql)
    if not toks:
        return sql
    selects, owner = _selects(toks)
    replace: dict[int, list[tuple[str, str]]] = {}
    drop: set[int] = set()
    inserts: dict[int, list[tuple[str, str]]] = {}
    joined: dict[tuple[int, str], str] = {}
    n = 0
    for i, t in enumerate(toks):
        if t[0] not in ("word", "qid") or fold(analysis_plan._name(*t)) != fold(PLACE_COLUMN):
            continue
        if i + 1 < len(toks) and toks[i + 1] == ("punct", "("):
            continue
        qualified = i >= 2 and toks[i - 1] == ("punct", ".") and toks[i - 2][0] in ("word", "qid")
        s_at = owner[i]
        if s_at < 0:
            continue
        s = selects[s_at]
        obs_aliases = [a for nm, a in s.sources if fold(nm) == fold(obs)]
        if not obs_aliases:
            continue
        alias = analysis_plan._name(*toks[i - 2]) if qualified else obs_aliases[0]
        if fold(alias) not in {fold(a) for a in obs_aliases}:
            continue
        place = joined.get((s_at, fold(alias)))
        if place is None:
            n += 1
            place = "_place" if n == 1 else f"_place{n}"
            joined[(s_at, fold(alias))] = place
            at = s.from_end if s.from_end is not None else len(toks)
            join = "JOIN" if _place_required(toks, s) else "LEFT JOIN"
            inserts.setdefault(at, []).extend(analysis_plan.tokens(
                f"{join} {area} {place} ON {_q(alias)}.{_q(key)} = {place}.{_q(oid)}") or [])
        new = [("word", place), ("punct", "."), ("word", _q(name))]
        # A column of the SELECT itself keeps its name (PLACE_NAME).
        start = i - 2 if qualified else i
        if s.select_end is not None and s.start < start < s.select_end and toks[start - 1] in (("kw", "SELECT"), ("kw", "DISTINCT"), ("punct", ",")) \
                and i + 1 < len(toks) and (toks[i + 1] == ("punct", ",") or toks[i + 1] == ("kw", "FROM")):
            new += [("kw", "AS"), ("word", PLACE_COLUMN)]
        if qualified:
            drop.update({i - 2, i - 1})
        replace[i] = new
    if not replace:
        return sql
    # The columns both tables have, named without a table (KIND): the records' own (they were before the join).
    aliases = {fold(analysis_plan._name(*toks[i + 1])) for i, t in enumerate(toks[:-1]) if t == ("kw", "AS") and toks[i + 1][0] in ("word", "qid")}
    for i, t in enumerate(toks):
        s_at = owner[i]
        if i in replace or i in drop or t[0] not in ("word", "qid") or s_at < 0:
            continue
        name = fold(analysis_plan._name(*t))
        if name not in schema.shared or name in aliases:
            continue
        if (i and toks[i - 1] in (("punct", "."), ("kw", "AS"))) or (i + 1 < len(toks) and toks[i + 1] in (("punct", "("), ("punct", "."))):
            continue
        s = selects[s_at]
        if s.select_end is None:
            continue
        mine = [a for (sel, a) in joined if sel == s_at]
        if not mine:
            continue
        alias = next(a for nm, a in s.sources if fold(a) == mine[0])
        if s.select_end < i < (s.from_end or len(toks)):
            continue  # in the FROM itself (a table's name or alias)
        replace[i] = [("word", _q(alias)), ("punct", "."), t]
    out: list[tuple[str, str]] = []
    for i, t in enumerate(toks):
        out.extend(inserts.get(i, []))
        if i in drop:
            continue
        out.extend(replace.get(i, [t]))
    out.extend(inserts.get(len(toks), []))
    return analysis_plan._text(out)


def _q(name: str) -> str:
    return name if re.fullmatch(r"[^\W\d]\w*", name) else '"' + name.replace('"', '""') + '"'


# ---- the answer --------------------------------------------------------------------------------------

def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if not v.is_integer() else str(int(v))
    return str(v)


def counted(columns: Sequence[str], rows: Sequence[Sequence[Any]], labels: Optional[dict[str, str]] = None) -> list[tuple[str, list[tuple[str, int]]]]:
    """
    What code counts for the answer: for each text column whose values
    repeat (2–8 kinds over more than one row), how many rows each value
    has (a path by its last class).
    """
    out = []
    if len(rows) < 2:
        return out
    for j, name in enumerate(columns):
        vals = [r[j] for r in rows]
        if not all(isinstance(v, str) for v in vals if v is not None) or all(v is None for v in vals):
            continue
        if any(re.fullmatch(r"\d{4}-\d{2}(-\d{2}.*)?", v or "") for v in vals):
            continue
        short = [(v.split(">")[-1].strip() if v and ">" in v else v) or "（空）" for v in vals]
        kinds = list(dict.fromkeys(short))
        if 2 <= len(kinds) <= 8 and len(kinds) < len(rows):
            out.append(((labels or {}).get(name.upper(), name), [(k, short.count(k)) for k in kinds]))
    return out


def answer_input(question: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], periods: Periods,
                 labels: Optional[dict[str, str]] = None) -> str:
    """What the answer is written from: the question, the result (at most ANSWER_ROWS rows, months and days labelled) and code's counts."""
    labels = labels or {}
    head = "\t".join(labels.get(c.upper(), c) for c in columns)
    lines = [head] + ["\t".join(_cell(v) + periods.label(v) for v in r) for r in rows[:ANSWER_ROWS]]
    if len(rows) > ANSWER_ROWS:
        lines.append(f"（ほか {len(rows) - ANSWER_ROWS} 行。全部で {len(rows)} 行）")
    text = f"質問: {question.strip()}\n\n結果の表（{len(rows)} 行）:\n" + "\n".join(lines)
    counts = counted(columns, rows, labels)
    if counts:
        text += "\n\nプログラムが数えた数: " + " / ".join(f"{c}: " + "・".join(f"{k} {n}" for k, n in kinds) for c, kinds in counts)
    return text


def answer_messages(question: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], periods: Periods,
                    labels: Optional[dict[str, str]] = None) -> list[Message]:
    return [{"role": "system", "content": ANSWER_PROMPT},
            {"role": "user", "content": answer_input(question, columns, rows, periods, labels)}]


def clean_answer(text: str) -> str:
    """The sentences without the model's thinking, fences, list marks and blank lines."""
    s = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    s = re.sub(r"^```.*$", "", s, flags=re.MULTILINE)
    s = re.sub(r"(?m)^\s*(?:[-・*]|\d+[.．)])\s*", "", s)
    return re.sub(r"\s*\n\s*", "", s.strip()) if s.count("\n") < 4 else s.strip()


def _nz(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _names_of(v: str) -> set[str]:
    out = {_nz(v)}
    if " > " in v or ">" in v:
        out.add(_nz(v.split(">")[-1]))
    if " " in v.strip():
        out.add(_nz(v.split(" ")[0]))
    m = re.fullmatch(r"(\d{4})-(\d{2})", v)
    if m:
        out |= {f"{int(m.group(2))}月", f"{m.group(1)}年{int(m.group(2))}月"}
    return {n for n in out if n}


def check_answer(question: str, sql: str, answer: str, rows: Sequence[Sequence[Any]]) -> list[str]:
    """
    What does not match the result in the answer (empty: it may be shown):
    1. a number that is no cell, row count, column total, sum or difference
       of two cells, count of a name, or number of the question (digits in
       ids, names, dates and times are not numbers);
    2. a number right after a row's name that is not of that row;
    3. a quoted 「…」 word or an id not in the table or the question;
    4. rows named out of the table's order when the SQL sorts.
    (The study's check: caught 9 of 10 wrong answers, one false alarm.)
    """
    flags: list[str] = []
    text = _nz(re.sub(r"(?m)^\s*\d+[.．、)]", "", answer)).replace(",", "")
    qtext = _nz(unicodedata.normalize("NFKC", question))
    num_cells = [float(v) for r in rows for v in r if isinstance(v, (int, float)) and not isinstance(v, bool)]
    cols = [[v for v in r if isinstance(v, int) and not isinstance(v, bool)] for r in rows]
    allowed = {round(v, 1) for v in num_cells} | {float(len(rows))}
    allowed |= {round(sum(c), 1) for c in zip(*cols) if c}
    small = [float(v) for c in cols for v in c][:40]
    allowed |= {round(a + b, 1) for a, b in itertools.combinations(small, 2)} | {round(abs(a - b), 1) for a, b in itertools.combinations(small, 2)}
    qnums = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", qtext)}
    strings = {_nz(str(v)) for r in rows for v in r if isinstance(v, str)}
    occ: list[tuple[int, int, int, str]] = []
    rows_of: dict[str, set[int]] = {}
    for i, r in enumerate(rows):
        for v in r:
            if isinstance(v, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", v):
                for n in _names_of(v):
                    rows_of.setdefault(n, set()).add(i)
    for n, rs in rows_of.items():
        for m in re.finditer(re.escape(n), text):
            for i in rs:
                occ.append((m.start(), m.end(), i, n))
    counts = {n: len(rs) for n, rs in rows_of.items()}
    for quoted in re.findall(r"「([^」]+)」", text):
        for w in [p for p in re.split(r"[（）()、・]", quoted) if p]:
            if w not in strings and w not in rows_of and w not in qtext and not any(w in s for s in strings):
                flags.append(f"表にない語「{w}」")
    for w in re.findall(r"[A-Z][A-Z0-9]+_\d{8}_\d{4}", text):
        if w not in strings:
            flags.append(f"表にない ID {w}")
    for m in re.finditer(r"\d+(?:\.\d+)?", text):
        s, e = m.span()
        before, after = text[max(0, s - 1):s], text[e:e + 1]
        if re.match(r"[A-Za-z_\-:/]", before) or re.match(r"[A-Za-z_\-:/]", after) or (after and after in "年月日時分秒"):
            continue
        v = float(m.group())
        if v in qnums:
            continue
        near = [o for o in occ if o[1] <= s and s - o[1] <= 8 and not re.search(r"[0-9。計]", text[o[1]:s])]
        if near and v != len(rows):
            pos = max(o[1] for o in near)
            here = [o for o in near if o[1] == pos]
            ok = any(round(v, 1) in {round(float(x), 1) for x in rows[o[2]] if isinstance(x, (int, float)) and not isinstance(x, bool)} for o in here)
            ok = ok or any(v == counts[o[3]] for o in here)
            if not ok:
                flags.append(f"取り違え {m.group()}" if round(v, 1) in allowed else f"表にない数 {m.group()}")
        elif round(v, 1) not in allowed:
            flags.append(f"表にない数 {m.group()}")
    if re.search(r"\bORDER\s+BY\b", sql or "", re.IGNORECASE) and len(rows) > 1:
        first: dict[int, int] = {}
        for pos, _e, i, n in sorted(occ):
            if counts[n] == 1:
                first.setdefault(i, pos)
        seq = [i for i, _p in sorted(first.items(), key=lambda x: x[1])]
        if seq != sorted(seq):
            flags.append("表と順が違う")
    if not rows and not re.search(r"ありません|なかった|ない|0", answer):
        flags.append("0 行なのに該当があるように書いた")
    return list(dict.fromkeys(flags))


def plain_answer(columns: Sequence[str], rows: Sequence[Sequence[Any]], labels: Optional[dict[str, str]] = None) -> str:
    """The sentence code writes (when the model's does not match, or there is none)."""
    if not rows:
        return "該当するものはありませんでした。"
    if len(rows) == 1 and len(columns) == 1:
        v = rows[0][0]
        name = columns[0]
        if re.match(r"(?i)\s*count\s*\(", name):
            return f"{_cell(v)} 件でした。"
        return f"{(labels or {}).get(name.upper(), name)}は {_cell(v)} でした。"
    return f"{len(rows):,} 行の結果です（下の表）。"


def header_label(name: str, labels: Optional[dict[str, str]] = None) -> str:
    """A result column's heading on the screen: its label (COUNT(*) as 件数), else its name."""
    labels = labels or {}
    if re.fullmatch(r"(?i)\s*count\s*\(\s*(\*|1)\s*\)\s*", name):
        return "件数"
    return labels.get(name.upper(), name)


# ---- when a sentence is a question for the data -------------------------------------------------------

_QUESTION = re.compile(
    r"[?？]|何件|何回|何枚|何隻|何機|何台|何人|何個|いくつ|件数|多い順|少ない順|多かった|少なかった|ごとに|ごとの|別に|割合|比べ|比較|平均|合計|"
    r"最大|最小|一番|最も|最新|最後に|誰|だれ|どれ|どこ|どの|いつ|ランキング|上位|推移|増え|減っ|あった\b|ありますか|ある\?|教えて")


def is_question(text: str) -> bool:
    """Whether a sentence of 文で操作 asks about the data (counts, lists, who, when …) rather than for an operation."""
    return bool(_QUESTION.search(unicodedata.normalize("NFKC", text or "")))


def describe(sql: str, ran: str, counts: dict[str, int], conditions: dict[str, str], notes: Iterable[str] = ()) -> str:
    """「どう調べたか」: the SQL the model wrote (and what was run, when code rewrote it), the tables read and how many rows each gave."""
    lines = ["SQL（生成 AI が書いたもの）:", sql.strip()]
    if ran.strip() != sql.strip():
        lines += ["", "実行した SQL（地名の列をプログラムが結合に直したもの）:", ran.strip()]
    if counts:
        lines += ["", "読んだ表: " + "、".join(f"{n} {c:,} 行" + (f"（条件 {conditions[n]}）" if conditions.get(n) else "")
                                           for n, c in counts.items())]
    lines += [n for n in notes if n]
    return "\n".join(lines)
