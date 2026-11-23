"""
文で操作 (command; tsudo 2026-11-21: 「生成AIに指示してある操作をするみたいなことをやれたらいいな…
〇〇システムの〇〇日のオーダーファイルを出力してとか」, study → 「この方針で 17 操作すべて」).

A sentence typed in the toolbar's 「文で操作」 field is turned into one of a
fixed list of operations (OPERATIONS): which screen to open and what to put
in it. The model only chooses the operation: its words matched first (one
operation whose words the sentence holds: no model), else the model is
shown the list (only the operations the roles in use have) and writes one
line 「操作: 名前」. Every value (the system, the day or period, the place,
the satellites, the screen) is read here from the sentence, by code, in the
direction the operation looks (計画ファイル・撮像計画 ahead, 画像検索・観測記録
back); one it cannot read is left empty, never guessed. Nothing runs
before the person looks at the card and presses its button, and nothing
is saved, written or sent by it (the screens' own buttons do that).

Measured on the real sarashina2.2 3B int4 (2026-11-21, 4 cores, the 33
sentences of tests/test_text_command.py and two that are no operation):
the operation right in 33 (27 by their words, 6 by the model in about a
second each once the list was read, 10 s), the values read by code right
in 33 (the model writing them got 21 of 32 right).

This module is pure Python: the operations, the prompt and the readers.
The card and the running are ui/text_command.py and features/command.py.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional, Sequence

from .text_assist import Message, TextTask
from .text_request import period_of
from .text_tasks import period_in
#: The most of a sentence read (characters).
TEXT_LIMIT = 300
#: The name of 「no operation」 in the model's list.
NONE = "なし"


@dataclass(frozen=True)
class Operation:
    """One thing 文で操作 can do: what the model is told of it, the words that name it and what it reads."""
    key: str
    #: Its name in the model's list (short, one of a kind).
    name: str
    #: One line for the model.
    describe: str
    #: The feature it belongs to (a menu entry's text, for the roles in use; "" for every role).
    feature: str
    #: Words that name it on their own (matched without the model when only one operation's words are found).
    words: tuple[str, ...]
    #: What it reads: system, day (one day), period (first and last), year, place, satellites, screen.
    slots: tuple[str, ...] = ()
    #: Which way its dates look when a word could be either (「今月」): True back (records, images), False ahead (plans).
    past: Optional[bool] = None
    #: The card's title and its button.
    title: str = ""
    button: str = "実行"
    #: What the card says will happen (and what is left to the person).
    note: str = ""


OPERATIONS: tuple[Operation, ...] = (
    Operation("planFiles", "計画ファイル出力", "撮像計画ファイル（オーダーファイル）をシステムごと・日ごとに作って書き出す", "撮像計画ファイル出力",
              ("オーダーファイル", "オーダー", "計画ファイル", "撮像計画ファイル", "発注ファイル"), ("system", "day"), False,
              "撮像計画ファイル出力", "開いて計算",
              "「撮像計画ファイル出力」を開き、対象日とシステムを入れて「計算」。書き出しは、計算の結果を見てから画面の「書き出し」で（いつもの確認あり）"),
    Operation("schedule", "撮像予定", "ある日の撮像予定（システムの採否・撮像済みか）を一覧で見る", "撮像予定",
              ("撮像予定", "採否", "不採用", "採用された"), ("day", "system"), None, "撮像予定", "表示",
              "「撮像予定」にその日を表示（システムを選べばそのシステムだけ）"),
    Operation("resultImport", "結果ファイル取込", "システムから返ってきた結果ファイル（採否）を撮像予定に取り込む", "撮像予定",
              ("結果ファイル", "採否の結果", "採否ファイル", "回答ファイル"), ("system", "day"), None, "結果ファイルの取り込み", "ファイルを選ぶ",
              "「撮像予定」を開き、そのシステムの結果ファイルを選ぶ画面を表示。取り込む前に、いつもの確認あり"),
    Operation("follow", "照合", "出力した依頼が撮像されたかを画像カタログと照合する", "撮像計画ファイル出力",
              ("照合", "撮れたか", "撮像されたか", "撮れているか"), ("period",), True, "計画と実績の照合", "開く",
              "「計画と実績の照合」を開いて期間を入力。照合は画面の「照合」で"),
    Operation("quotas", "年間上限", "年間撮像上限の消費と残りを見る", "年間撮像上限…",
              ("年間上限", "年間撮像上限", "撮像上限", "上限の残り"), ("year",), True, "年間撮像上限", "開く",
              "「年間撮像上限」をその年で表示"),
    Operation("catalog", "画像検索", "画像カタログで撮像済みの画像を探す（場所・期間）", "画像カタログ",
              ("画像を探", "画像検索", "画像カタログ", "撮った画像", "画像はある", "画像があるか"), ("place", "period"), True, "画像カタログ", "検索",
              "「画像カタログ」で、その場所（なければ地図の表示範囲）と期間で検索"),
    Operation("passes", "衛星パス", "衛星の軌道・通過を地図に描く", "衛星パス",
              ("衛星パス", "軌道", "パスを", "通過"), ("satellites", "day"), None, "衛星パス", "描く",
              "「衛星パス」でその衛星を選び、その日（なければ今）の軌道を描画"),
    Operation("placeSchedule", "地点の撮像予定", "ある地点のこれからの撮像予定をタイムラインで見る", "地点ごとの撮像予定",
              ("タイムライン", "地点ごとの撮像予定", "地点の撮像予定"), ("place",), False, "地点ごとの撮像予定", "表示",
              "「地点ごとの撮像予定」にその地点を追加"),
    Operation("imagingPlan", "撮像計画", "ある地点をいつどの衛星で撮れるか計算する", "撮像計画",
              ("撮れる衛星", "撮像機会", "撮像計画", "いつ撮れる", "撮れるか"), ("place", "period"), False, "撮像計画", "計算",
              "「撮像計画」でその地点を対象にし、期間を入れて計算"),
    Operation("watch", "新着の監視", "ある地点・地域に新しい画像が入ったら知らせるようにする", "通知",
              ("新着", "監視", "知らせて", "通知して"), ("place",), None, "新着画像の監視", "監視の画面を開く",
              "その地点の「新着画像を監視」の画面を表示。登録は画面で"),
    Operation("obsExport", "観測記録の書き出し", "観測記録を条件で絞ってファイル（CSV など）に書き出す", "観測記録のエクスポート…",
              ("エクスポート", "観測記録を出力", "観測記録の出力", "観測記録を書き出", "記録をcsv"), ("period",), True, "観測記録のエクスポート",
              "条件を入れて開く", "「観測記録のエクスポート」を開き、文から読める条件（期間・地名・大分類・確度・有無）を入力。保存は画面で"),
    Operation("handover", "引き継ぎメモ", "交代のときの引き継ぎメモ（申し送り）を作る", "引き継ぎメモ…",
              ("引き継ぎ", "引継ぎ", "引継", "申し送り"), (), None, "引き継ぎメモ", "開く", "「引き継ぎメモ」を表示"),
    Operation("alert", "通報", "注目の記録から通報文を作る", "通報文の作成…",
              ("通報",), (), None, "通報文の作成", "開く", "「通報文の作成」を表示。送信は画面で"),
    Operation("newRequest", "依頼の作成", "新しい撮像依頼を作る", "撮像依頼",
              ("依頼を作", "依頼を登録", "依頼を出し", "撮像依頼を作", "撮像を依頼"), (), False, "撮像依頼の登録", "新規の依頼を開く",
              "新しい撮像依頼の画面を表示。保存は画面で"),
    Operation("outage", "障害情報", "衛星の障害（観測停止など）を登録・確認する", "衛星の障害情報…",
              ("障害", "観測停止", "運用停止", "故障"), ("satellites",), None, "衛星の障害情報", "登録の画面を開く",
              "「衛星の障害情報」の登録画面を開き、衛星と文を入力。保存は画面で"),
    Operation("jump", "移動", "地図を地名・座標の場所へ移動する", "",
              ("移動", "ジャンプ", "飛んで"), ("place",), None, "座標・地名へ移動", "移動", "地図をその場所へ移動"),
    Operation("openScreen", "画面を開く", "上にないほかの画面（画像補正・物体検出・データ分析など）を開く", "",
              ("画面を開", "画面を出"), ("screen",), None, "画面を開く", "開く", "その画面を表示"),
)
BY_KEY = {o.key: o for o in OPERATIONS}

#: Examples shown before the sentence (operation key, sentence), those of operations in the list.
EXAMPLES: tuple[tuple[str, str], ...] = (
    ("planFiles", "Bシステムの明日分のオーダーファイルを作って"),
    ("jump", "横須賀に移動して"),
    ("catalog", "那覇の昨日の画像を見たい"),
)


def fold(text: str) -> str:
    """NFKC, lower case, without spaces (the words compared)."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


# ---- the operations the words name -----------------------------------------------------------------

def word_matches(text: str, ops: Sequence[Operation], more: Optional[dict] = None) -> list[Operation]:
    """
    The operations whose words `text` holds (`more`: key → more words of the
    settings). A word found inside a longer word of another operation found
    (撮像計画 in 撮像計画ファイル) is not counted.
    """
    t = fold(text)
    found: dict[str, list[str]] = {}
    for o in ops:
        words = list(o.words) + list((more or {}).get(o.key, ()))
        hits = [fold(w) for w in words if fold(w) and fold(w) in t]
        if hits:
            found[o.key] = hits
    keys = list(found)
    out = []
    for k in keys:
        inside = all(any(h != g and h in g for other in keys if other != k for g in found[other]) for h in found[k])
        if not inside:
            out.append(k)
    return [o for o in ops if o.key in out]


def by_words(text: str, ops: Sequence[Operation], more: Optional[dict] = None, screens: Sequence[str] = ()) -> Optional[Operation]:
    """
    The one operation the words name (None: none, or several — the model
    chooses); with no operation's words, a screen of the menu named
    (`screens`; 「物体検出したい」) is 画面を開く.
    """
    found = word_matches(text, ops, more)
    if not found and screens and any(o.key == "openScreen" for o in ops) and screen_in(text, screens):
        return BY_KEY["openScreen"]
    return found[0] if len(found) == 1 else None


# ---- the model --------------------------------------------------------------------------------------

def op_list(ops: Sequence[Operation]) -> str:
    return "\n".join(f"- {o.name}: {o.describe}" for o in ops) + f"\n- {NONE}: 上のどれにも当たらない・MIKAGE の操作ではない"


def command_messages(task: TextTask, ops: Sequence[Operation], text: str) -> list[Message]:
    """
    The conversation for the model: the instructions and the list, the
    examples of operations in the list, then the sentence. Everything before
    the sentence stays the same while the list does (the model keeps it read).
    """
    messages: list[Message] = [{"role": "system", "content": f"{task.prompt}\n\n操作の一覧:\n{op_list(ops)}"}]
    keys = {o.key for o in ops}
    for key, sentence in EXAMPLES:
        if key in keys:
            messages += [{"role": "user", "content": f"依頼: {sentence}"}, {"role": "assistant", "content": f"操作: {BY_KEY[key].name}"}]
    messages.append({"role": "user", "content": f"依頼: {text.strip()[:TEXT_LIMIT]}"})
    return messages


def operation_of(raw: str, ops: Sequence[Operation]) -> Optional[Operation]:
    """
    The operation the model's reply names (its 「操作:」 line); None for
    「なし」, a name not in the list, or a reply without one (an apology).
    """
    names = sorted(ops, key=lambda o: len(o.name), reverse=True)
    for line in raw.replace("\r\n", "\n").split("\n"):
        m = re.match(r"\s*操作\s*[:：]\s*(.*)$", line)
        if not m:
            continue
        value = fold(m[1])
        for o in names:
            if value.startswith(fold(o.name)):
                return o
        return None
    return None


def reply_done(ops: Sequence[Operation]):
    """When the model has written enough: a line ended, or an operation's whole name after 「操作:」."""
    names = [fold(o.name) for o in ops] + [fold(NONE)]

    def done(text: str) -> bool:
        if "\n" in text.lstrip():
            return True
        m = re.search(r"操作\s*[:：]\s*(.*)$", text.strip())
        return bool(m) and any(fold(m[1]).startswith(n) for n in names)

    return done


# ---- what the sentence says -------------------------------------------------------------------------

#: All the systems (「全システム」「すべてのシステム」).
ALL = "*"
_ALL_SYSTEMS = re.compile(r"全システム|すべてのシステム|全てのシステム|全部のシステム|各システム|全系統")
_AFTER_LETTER = r"(?:システム|系|の|と|向け|から|へ|が|は|分|$)"


def _base(label: str) -> str:
    """A system's label without what is in brackets (「A システム（光学）」 → 「aシステム」)."""
    return fold(re.split(r"[（(]", label, 1)[0])


def systems_in(text: str, systems: Sequence[Any]) -> list[str]:
    """
    The systems (`requestExports`: .system, .label) `text` names, in the
    settings' order: by code (SYS-A), by label (「A システム」), or a label's
    short head before 「システム」 (「A」 of 「A システム」) followed by
    システム・系・の・と… (「B系」「Bの」); [ALL] for 「全システム」.
    """
    t = fold(text)
    if _ALL_SYSTEMS.search(t):
        return [ALL]
    out = []
    for s in systems:
        code, base = fold(s.system), _base(s.label)
        if (code and re.search(rf"(?<![a-z0-9]){re.escape(code)}(?![a-z0-9])", t)) or (len(base) >= 2 and base in t):
            out.append(s.system)
            continue
        # 「A システム」's 「A」, or a short label itself (「A」).
        head = base[:-len("システム")] if base.endswith("システム") else base if len(base) <= 3 else ""
        if head and len(head) <= 3 and re.search(rf"(?<![a-z0-9]){re.escape(head)}{_AFTER_LETTER}", t):
            out.append(s.system)
    return out


_PAST_WORDS = re.compile(r"昨日|一昨日|おととい|先週|先月|直近|過去|去年|昨年|前年|この前|以前")


def period_in_text(text: str, today: date, past: Optional[bool]) -> Optional[tuple[date, date]]:
    """
    The day or days `text` names (first, last); None when it names none.
    `past`: True for an operation looking back (「今月」 = the 1st to the
    end), False ahead (「今月」 = today to the end); None: by the words.
    """
    t = unicodedata.normalize("NFKC", text)
    back = past if past is not None else bool(_PAST_WORDS.search(t))

    def behind():
        return period_in(t, today)

    def ahead():
        p = period_of(t, today)
        if p.start or p.end:
            a, b = p.start or p.end, p.end or p.start
            return (a, b) if a <= b else (b, a)
        return None

    for read in ((behind, ahead) if back else (ahead, behind)):
        found = read()
        if found:
            return found
    return None


_LAST_YEAR = re.compile(r"去年|昨年|前年")


def year_back(text: str) -> int:
    """How many years back 「年間上限」 is asked for (去年: 1)."""
    return 1 if _LAST_YEAR.search(text) else 0


#: 「35.3, 139.6」「35.3 139.6」 (latitude, longitude in degrees).
_LAT_LON = re.compile(r"(?<![\d.])([-+]?\d{1,2}(?:\.\d+)?)\s*[,、，\s]\s*([-+]?\d{1,3}(?:\.\d+)?)(?![\d.])")


def lat_lon_in(text: str) -> Optional[tuple[float, float]]:
    """A latitude and longitude written in `text` as (lon, lat); None when there is none."""
    t = unicodedata.normalize("NFKC", text)
    for m in _LAT_LON.finditer(t):
        lat, lon = float(m[1]), float(m[2])
        if ("." in m[1] or "." in m[2]) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return lon, lat
    return None


#: Words before の・で・を・へ・に that are no place.
_NOT_PLACES = {
    "今日", "明日", "明後日", "昨日", "一昨日", "今週", "来週", "先週", "再来週", "今月", "来月", "先月", "今年", "去年", "昨年",
    "年内", "直近", "最近", "過去", "毎日", "全部", "全て", "すべて", "選んだ記録", "選んだ", "交代", "画面", "地図", "衛星",
    "画像", "記録", "依頼", "撮像", "予定", "計画", "結果", "システム", "観測記録", "撮像予定", "撮像計画", "撮像依頼", "今", "次",
    "前", "後", "分", "日", "週", "月", "年", "上限", "年間上限", "新着", "通報", "障害", "軌道", "雲", "艦船", "船舶", "航空機", "車両",
}
_CHUNK = re.compile(r"([一-鿿々〆ヵヶ゠-ヿー・A-Za-z0-9\-]{1,20}?)(?:周辺|付近|近く|一帯|地区)?(?=の|で|を|へ|に|まで|周辺|付近|近く|、|$)")


def place_word(text: str, known: Sequence[str] = (), others: Sequence[str] = ()) -> Optional[str]:
    """
    The place `text` names: one of `known` (named areas, interests; the
    longest found), else the first word before の・で・を・へ・に that is
    not a date, a system, an operation's word or a common word (`others`:
    more words that are no place).
    """
    t = unicodedata.normalize("NFKC", text)
    folded = fold(t)
    said = sorted((k for k in known if fold(k) and fold(k) in folded), key=lambda k: len(fold(k)), reverse=True)
    if said:
        return said[0]
    stop = {fold(w) for w in _NOT_PLACES} | {fold(w) for w in others}
    op_words = [fold(w) for o in OPERATIONS for w in o.words]
    for m in _CHUNK.finditer(re.sub(r"\s+", "", t)):
        word = m[1].strip("・-")
        f = fold(word)
        if not f or f in stop or f.isdigit() or re.fullmatch(r"[\d.\-]+", f):
            continue
        if "システム" in word or re.search(r"\d+(日|月|年|週)", word) or re.fullmatch(r"[a-z]{1,3}", f):
            continue
        if any(w and (w in f or f in w) for w in op_words):
            continue
        if re.fullmatch(r"[぀-ゟ]+", word):
            continue
        return word
    return None


def screen_in(text: str, screens: Sequence[str]) -> Optional[str]:
    """The screen (a menu entry's text) `text` names: the longest name found, without 「…」 and brackets."""
    t = fold(text)

    def short(name: str) -> str:
        return fold(re.sub(r"[（(][^）)]*[）)]|…", "", name))

    found = [s for s in screens if short(s) and short(s) in t]
    if not found:
        # 「物体検出したい」 for 「物体検出（AI）…」: the name's head of 3 characters or more.
        found = [s for s in screens if len(short(s)) >= 3 and any(short(s)[:n] in t for n in range(len(short(s)), 2, -1))]
    return max(found, key=lambda s: len(short(s))) if found else None


@dataclass
class Reading:
    """What a sentence was read as: the operation and its values (None: not read; the card leaves it empty)."""
    operation: Operation
    text: str
    #: System codes (ALL for every system), in the settings' order.
    systems: list[str] = field(default_factory=list)
    period: Optional[tuple[date, date]] = None
    year_back: int = 0
    place: Optional[str] = None
    #: A latitude and longitude written (lon, lat).
    point: Optional[tuple[float, float]] = None
    satellites: list[str] = field(default_factory=list)
    screen: Optional[str] = None
    #: How the operation was found: "words" (no model) or "model".
    by: str = "words"

    @property
    def day(self) -> Optional[date]:
        return self.period[0] if self.period else None

    def missing(self) -> list[str]:
        """The values the operation needs that were not read (the card asks for them)."""
        o, out = self.operation, []
        if o.key in ("placeSchedule", "imagingPlan", "watch", "jump") and not (self.place or self.point):
            out.append("place")
        if o.key == "planFiles" and self.period is None:
            out.append("day")
        if o.key == "openScreen" and not self.screen:
            out.append("screen")
        return out


def read(op: Operation, text: str, today: date, systems: Sequence[Any] = (), places: Sequence[str] = (),
         sats: Sequence[Any] = (), screens: Sequence[str] = (), by: str = "words") -> Reading:
    """
    The values of `op` that `text` says, read by code (`systems`:
    requestExports; `places`: names known; `sats`: the satellite catalog;
    `screens`: the menu's entries).
    """
    r = Reading(op, text, by=by)
    slots = set(op.slots)
    if "system" in slots:
        r.systems = systems_in(text, systems)
    if slots & {"day", "period"}:
        r.period = period_in_text(text, today, op.past)
        if "day" in slots and "period" not in slots and r.period and r.period[0] != r.period[1]:
            # One day asked (計画ファイル・撮像予定): the first day of the period named.
            r.period = (r.period[0], r.period[0])
    if "year" in slots:
        r.year_back = year_back(text)
    if "place" in slots:
        r.point = lat_lon_in(text)
        if r.point is None:
            others = [s.system for s in systems] + [_base(s.label) for s in systems]
            others += [getattr(s, "name", "") for s in sats]
            r.place = place_word(text, places, others)
    if "satellites" in slots and sats:
        from .text_request import satellites_of

        named = satellites_of(text, sats)
        # A series named: its satellites (衛星パス chooses satellites).
        more = [x.name for x in sats if getattr(x, "series", None) in named.series and x.name not in named.satellites]
        r.satellites = list(dict.fromkeys([*named.satellites, *more]))
    if "screen" in slots:
        r.screen = screen_in(text, screens)
    return r


def day_text(d: Optional[date]) -> str:
    return f"{d.isoformat()}（{'月火水木金土日'[d.weekday()]}）" if d else ""


def period_days(period: Optional[tuple[date, date]]) -> int:
    """How many days a period holds (1 for one day, 0 for none)."""
    return (period[1] - period[0]).days + 1 if period else 0


def jst_bounds(d: date) -> tuple[float, float]:
    """A day in Japan time as epoch milliseconds (its first moment, the next day's)."""
    from datetime import datetime, timezone

    jst = timezone(timedelta(hours=9))
    start = datetime(d.year, d.month, d.day, tzinfo=jst)
    return start.timestamp() * 1000, (start + timedelta(days=1)).timestamp() * 1000


def extent_of(rings: Sequence) -> Optional[tuple[float, float, float, float]]:
    """The extent (west, south, east, north) of rings ([[[lon, lat], …], …]); None when there is no point."""
    points = [p for ring in rings for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not points:
        return None
    xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def center_of(rings: Sequence) -> Optional[tuple[float, float]]:
    """The middle of rings' extent (lon, lat)."""
    e = extent_of(rings)
    return ((e[0] + e[2]) / 2, (e[1] + e[3]) / 2) if e else None


def corners_of(rings: Sequence) -> Optional[list]:
    """The corners of rings' extent, as the catalog's search takes an area ([[lon, lat] × 4])."""
    e = extent_of(rings)
    if e is None:
        return None
    w, s, east, n = e
    return [[w, s], [east, s], [east, n], [w, n]]
