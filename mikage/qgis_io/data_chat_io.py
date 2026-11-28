"""
「ミカゲと話す」's model in QGIS (core/chat.py, core/data_chat.py; データに質問
until 2026-11-28): the settings file's tables and their columns (their
layers' information), the values named to the model (read from the tables),
and one question asked through:

1. the model writes one SELECT (the SQL conversation kept read: the next
   question reads only itself, GenAiWriter keep);
2. code checks it (one SELECT, only the tables offered) and makes
   PLACE_NAME a join;
3. 「データ分析」's engine reads what it needs (records deleted left out by
   the table's read condition) and runs it, read-only;
4. an SQL that fails is given back to the model once with its error;
5. the model says the result in 1–3 sentences; code checks them against
   the table and writes its own sentence when they do not match.

Every question is recorded in the audit log (kind chat): the question, the
SQL, the tables read and their rows, the model; never what the rows hold.

A sentence that is no question and that no operation's words name, or a
question the model says the tables cannot answer (「NG: …」), has the model
choose one of the operations instead (pick_operation: 文で操作's prompt and
list, core/text_command.py), read apart from the SQL conversation (not
kept: the next question still finds the tables read).
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from qgis.PyQt.QtCore import QObject

from ..core import audit
from ..core import data_chat as dc
from ..core import text_command as tc
from ..core.analysis import DEFAULT_TABLE_LIMIT, AnalysisError, Result, Table, esri_columns, esri_tables
from ..core.esri import EsriError
from ..core.text_assist import COMMAND_PROMPT, COMMAND_TOKENS
from ..core.util import js_string
from . import analysis_io
from .tasks import run_task

#: How long the columns and values read are kept (s): the tables change their columns rarely.
KEEP_SECONDS = 1800
JST = timezone(timedelta(hours=9))


@dataclass
class Answer:
    """One question answered (or not): what the dock shows on its card."""
    question: str
    #: The sentence shown (the model's when it matched the table, else code's).
    sentence: str = ""
    #: Whether the sentence is the model's.
    by_model: bool = False
    result: Optional[Result] = None
    #: The SQL the model wrote, and what was run (PLACE_NAME made a join).
    sql: str = ""
    ran: str = ""
    #: Rows each table gave, and the conditions it was read with.
    counts: dict = field(default_factory=dict)
    conditions: dict = field(default_factory=dict)
    #: What happened on the way (one try more, the answer's check, the model's size).
    notes: list = field(default_factory=list)
    #: Why there is no table ("" when there is one): the model said it cannot, the SQL is refused, the read failed.
    refused: str = ""
    #: The model said the tables cannot answer it (「NG: …」): an operation may be what was meant.
    cannot: bool = False
    seconds: float = 0.0
    #: The columns' labels (for the table's headings).
    labels: dict = field(default_factory=dict)

    def describe(self) -> str:
        """「どう調べたか」."""
        return dc.describe(self.sql, self.ran, self.counts, self.conditions, self.notes) if self.sql else "\n".join(self.notes)


class DataChat(QObject):
    """The model of 「ミカゲと話す」 (one ask at a time): the tables known, the conversation, the model asked through TextAssistant."""

    def __init__(self, context, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.context = context
        self.assistant = context.text_assist
        #: The earlier questions answered (for a follow-up), at most core/data_chat.HISTORY used.
        self.history: list[dc.Turn] = []
        self._schema: Optional[dc.Schema] = None
        self._values: dict = {}
        self._read_at = 0.0
        self._config_key: Any = None
        self._busy = False
        self._cancelled = False
        self._asked = None
        self._task = None
        context.configChanged.connect(self.forget)

    # ---- the tables ----------------------------------------------------------------------------------

    @property
    def offered(self) -> bool:
        return self.assistant.has(dc.TASK)

    @property
    def busy(self) -> bool:
        return self._busy

    def task(self):
        s = self.assistant.settings
        return s.task(dc.TASK) if s is not None else None

    def words(self) -> dict:
        """The settings' more words naming an operation (`textAssist.tasks.chat.words`)."""
        t = self.task()
        return dict(t.words) if t is not None else {}

    def tables(self) -> list[Table]:
        t = self.task()
        return dc.offered_tables(esri_tables(self.context.config), t.tables if t is not None else ())

    def forget(self) -> None:
        """The settings changed: the tables are read again at the next question (the conversation goes on)."""
        self._schema, self._values, self._read_at = None, {}, 0.0

    def new_conversation(self) -> None:
        self.history = []

    def _periods(self) -> dc.Periods:
        return dc.Periods(datetime.now(JST).replace(tzinfo=None))

    def read_tables(self, done: Callable[[dc.Schema], None], failed: Callable[[str], None]) -> None:
        """The tables' columns and values (kept KEEP_SECONDS), in a task; `done(schema)`."""
        if self._schema is not None and time.monotonic() - self._read_at < KEEP_SECONDS:
            done(self._schema)
            return
        tables = self.tables()
        if not tables:
            failed(self.tr("質問できる表がない（設定ファイルに観測記録・撮像依頼などの Esri の表がない）［E703］"))
            return
        client = self.context.esri()
        asks = [(t, None) for t in tables]

        def work(task):
            values: dict = {}
            problems = []
            for t, token in asks:
                if task.isCanceled():
                    raise AnalysisError("取消")
                try:
                    info = client.layer(t.url, token)
                    fields = [f for f in info.raw.get("fields") or [] if isinstance(f, dict) and isinstance(f.get("name"), str)]
                    t.columns = esri_columns(t, fields)
                    t.spatial = bool(info.geometry_type)
                except EsriError as error:
                    problems.append(f"{t.name}: {error}")
                    continue
                values.update(_read_values(client, t, token))
            return values, problems

        def over(answer) -> None:
            values, problems = answer
            ok = [t for t in tables if t.columns]
            if not ok:
                failed(self.tr("表を読めない: {p}").format(p="、".join(problems)))
                return
            self._values = values
            self._schema = dc.schema_of(ok, values)
            self._read_at = time.monotonic()
            done(self._schema)

        self._task = run_task(self.tr("ミカゲと話す: 表の確認"), work, over, failed, hidden=True)

    def warm(self) -> None:
        """The model loaded and the instructions read while the question is typed (a computer's model; quietly)."""
        t = self.task()
        if t is None or self._busy or not self.offered or self.assistant.on_server(dc.TASK):
            return

        def ready(schema: dc.Schema) -> None:
            if not self._busy:
                self.assistant.prime(dc.TASK, dc.sql_messages(t, schema, self._periods(), "", (), self._values))

        self.read_tables(ready, lambda _m: None)

    # ---- one question --------------------------------------------------------------------------------

    def cancel(self) -> None:
        self._cancelled = True
        if self._asked is not None:
            self._asked.cancel()
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass

    def stage(self) -> str:
        """What is happening now (the model's stage while it writes)."""
        return self._asked.stage if self._asked is not None else ""

    def ask(self, question: str, done: Callable[[Answer], None], stage: Callable[[str], None] = lambda _s: None) -> bool:
        """Answers `question` (in tasks); `done(answer)` once (also when it cannot: answer.refused). False when busy or not offered."""
        question = question.strip()
        t = self.task()
        if not question or t is None or self._busy or not self.offered:
            return False
        self._busy, self._cancelled = True, False
        started = time.monotonic()
        answer = Answer(question)

        def finish() -> None:
            self._busy, self._asked, self._task = False, None, None
            answer.seconds = time.monotonic() - started
            self._audit(answer)
            done(answer)

        def refuse(why: str) -> None:
            answer.refused = why
            finish()

        def with_schema(schema: dc.Schema) -> None:
            if self._cancelled:
                refuse(self.tr("取消"))
                return
            answer.labels = dict(schema.labels)
            periods = self._periods()
            messages = dc.sql_messages(t, schema, periods, question, self.history, self._values)
            stage(self.tr("生成 AI が SQL を作成中…"))
            self._write_sql(schema, periods, messages, answer, refuse, finish, stage, retried=False)

        stage(self.tr("表を確認中…"))
        self.read_tables(with_schema, refuse)
        return True

    def _write_sql(self, schema: dc.Schema, periods: dc.Periods, messages: list, answer: Answer, refuse, finish, stage,
                   retried: bool) -> None:
        def written(raw: str, _seconds: float) -> None:
            if self._cancelled:
                refuse(self.tr("取消"))
                return
            sql, why = dc.sql_of(raw)
            if sql is None:
                answer.sql = ""
                answer.cannot = not retried
                refuse(self.tr("この質問には表から答えられない（生成 AI: {w}）").format(w=why or raw.strip()[:80] or self.tr("答えなし")))
                return
            answer.sql = sql
            try:
                dc.check_sql(sql, schema.names)
            except dc.ChatError as error:
                again(raw, str(error))
                return
            answer.ran = dc.expand_place(sql, schema)
            stage(self.tr("表を読み込み・集計中…"))
            self._run(schema, answer.ran, answer, lambda: self._write_answer(schema, periods, answer, finish, stage),
                      lambda message, sqlite: again(raw, message) if sqlite else refuse(message))

        def again(raw: str, error: str) -> None:
            if retried or self._cancelled:
                answer.notes.append(self.tr("SQL を直せなかった: {e}").format(e=error))
                refuse(self.tr("SQL を実行できない: {e}").format(e=error))
                return
            answer.notes.append(self.tr("1 回目の SQL がエラー（{e}）。エラーを伝えて書き直させた").format(e=error))
            stage(self.tr("生成 AI が SQL を直し中…"))
            self._write_sql(schema, periods, dc.retry_messages(messages, raw, error), answer, refuse, finish, stage, retried=True)

        def failed(message: str) -> None:
            refuse(self.tr("取消") if message == "取消" else self.tr("生成 AI で作れない: {m}").format(m=message))

        t = self.task()
        self._asked = self.assistant.ask(dc.TASK, self.tr("ミカゲと話す"), messages, written, failed, keep=not retried,
                                         max_tokens=t.max_tokens if t is not None else None)
        if self._asked is None:
            self._busy = False

    def _run(self, schema: dc.Schema, sql: str, answer: Answer, then: Callable[[], None],
             failed: Callable[[str, bool], None]) -> None:
        """Runs `sql` with 「データ分析」's engine (records deleted left out); `failed(message, whether SQLite refused the SQL)`."""
        tables = [s.table for s in schema.shown]
        conditions = dict(schema.conditions)
        try:
            prepared = analysis_io.prepare(tables, sql, conditions, DEFAULT_TABLE_LIMIT)
        except AnalysisError as error:
            failed(str(error), True)
            return
        client = self.context.esri()
        minutes = getattr(self.context.config, "analysis_cache_minutes", 0) or 0

        def work(task):
            try:
                return analysis_io.run(client, prepared, sql, {}, conditions, DEFAULT_TABLE_LIMIT,
                                       task, minutes), None
            except AnalysisError as error:
                # SQLite's own refusal (no such column …) is given back to the model; a read that failed is not.
                return None, (str(error), not getattr(error, "code", ""))

        def over(found) -> None:
            self._task = None
            ran, problem = found
            if problem is not None:
                failed(*problem)
                return
            result, counts, _notes = ran
            answer.result = result
            answer.counts = dict(counts)
            answer.conditions = {n: c for n, c in conditions.items() if n in counts}
            then()

        self._task = run_task(self.tr("ミカゲと話す: 読み込み"), work, over, lambda m: failed(m, False))

    def _write_answer(self, schema: dc.Schema, periods: dc.Periods, answer: Answer, finish, stage) -> None:
        result = answer.result
        columns, rows = list(result.columns), list(result.rows)
        plain = dc.plain_answer(columns, rows, schema.labels)
        caution = self.assistant.caution(dc.TASK)
        if caution:
            answer.notes.append(caution)

        def written(raw: str, _seconds: float) -> None:
            text = dc.clean_answer(raw)
            flags = dc.check_answer(answer.question, answer.ran, text, rows) if text else [self.tr("文がない")]
            if flags:
                answer.sentence, answer.by_model = plain, False
                answer.notes.append(self.tr("生成 AI の文が表と合わないため、プログラムの文にした（{f}）。生成 AI の文: {t}").format(
                    f="・".join(flags), t=text[:200]))
            else:
                answer.sentence, answer.by_model = text, True
            self.history.append(dc.Turn(answer.question, answer.sql))
            finish()

        def failed(message: str) -> None:
            answer.sentence, answer.by_model = plain, False
            if message != "取消":
                answer.notes.append(self.tr("答えの文を作れなかった（{m}）。プログラムの文にした").format(m=message))
            self.history.append(dc.Turn(answer.question, answer.sql))
            finish()

        if self._cancelled:
            failed("取消")
            return
        stage(self.tr("生成 AI が答えの文を作成中…"))
        messages = dc.answer_messages(answer.question, columns, rows, periods, schema.labels)
        self._asked = self.assistant.ask(dc.TASK, self.tr("ミカゲと話す"), messages, written, failed, max_tokens=dc.ANSWER_TOKENS)

    # ---- one operation chosen ----------------------------------------------------------------------------

    def pick_operation(self, text: str, ops: list, done: Callable[[Optional[tc.Operation], str, float], None],
                       failed: Callable[[str], None]) -> bool:
        """
        The operation of `ops` the model chooses for `text` (「操作: 名前」, 文で操作's prompt and list), in a task:
        `done(operation or None, the model's reply, seconds)`; read apart (not kept: the SQL conversation stays read).
        False when busy or not offered (nothing told).
        """
        t = self.task()
        if t is None or self._busy or not self.offered or not ops:
            return False
        self._busy, self._cancelled = True, False
        started = time.monotonic()

        def written(raw: str, _seconds: float) -> None:
            self._busy, self._asked = False, None
            done(None if self._cancelled else tc.operation_of(raw, ops), raw, time.monotonic() - started)

        def not_written(message: str) -> None:
            self._busy, self._asked = False, None
            failed(message)

        messages = tc.command_messages(dataclasses.replace(t, prompt=COMMAND_PROMPT), ops, text)
        self._asked = self.assistant.ask(dc.TASK, self.tr("ミカゲと話す"), messages, written, not_written, one_line=True,
                                         until=tc.reply_done(ops), keep=False, max_tokens=COMMAND_TOKENS)
        # None: it could not start (`failed` told already).
        return True

    def _audit(self, answer: Answer) -> None:
        s = self.assistant.settings
        model = s.model_for(dc.TASK) if s is not None else None
        detail = {"sql": answer.ran or answer.sql, "tables": dict(answer.counts), "model": model.name if model is not None else "",
                  "sentence": "model" if answer.by_model else "program", "seconds": round(answer.seconds, 1)}
        if answer.refused:
            detail["refused"] = answer.refused
        rows = len(answer.result.rows) if answer.result is not None else None
        audit.record("chat", "question", answer.question, result="ok" if not answer.refused else "error", count=rows, detail=detail)


def _read_values(client: Any, table: Table, token: Optional[str]) -> dict:
    """The values of `table`'s columns named to the model (core/data_chat.NOTES read=True): {(table, column): [values]}."""
    out: dict = {}
    notes = dc.NOTES.get(table.kind, ("", ()))[1]
    have = {c.name.upper(): c.name for c in table.columns}
    for col in notes:
        if not col.read:
            continue
        name = table.roles.get(col.role)
        if not name or name.upper() not in have:
            continue
        name = have[name.upper()]
        try:
            answer = client.ask(f"{table.url}/query", {
                "where": table.where or "1=1", "outFields": name, "returnDistinctValues": "true", "returnGeometry": "false",
                "orderByFields": name,
            }, token)
        except (EsriError, OSError, ValueError):
            continue
        found = []
        for f in answer.get("features") or []:
            v = (f.get("attributes") or {}).get(name)
            text = js_string(v).strip() if v is not None else ""
            if text and text not in found:
                found.append(text)
            if len(found) > 200:
                break
        if found and (col.path or len(found) <= dc.VALUES_LIMIT * 4):
            out[(table.name, name)] = sorted(found)
    return out


def columns_of(answer: Answer) -> list[str]:
    """The result's headings: labels where known (COUNT(*) as 件数)."""
    return [dc.header_label(c, answer.labels) for c in (answer.result.columns if answer.result is not None else [])]


def opened_in_analysis(dock: Any, answer: Answer, schema_conditions: dict) -> None:
    """「データ分析で開く」: the SQL run (and the tables' read conditions) put in the dock 「データ分析」."""
    for name, condition in schema_conditions.items():
        old = dock.conditions.get(name, "")
        if condition and condition not in old:
            dock.edit_condition(name, f"({old}) AND {condition}" if old else condition)
    dock.set_sql(answer.ran or answer.sql)
