"""
文章の手伝い（小さな生成 AI）: a small generative model run on this computer
(tsudo 2026-11-19: 「本当に小さい機能でいいので、量子化されたミニ生成AI機能
みたいなの同梱できないか検討してみて」 → 「両方（備考を先に）」, model
「sarashina2.2-1Bで良いよ」).

What it is asked to do (a task), each only proposals that a person reads and
takes or leaves:

- 備考を整える (tidyNote): a 備考 typed in the observation record (or checked
  area) form, written again in the plain style of a reading record, with
  the words of the 備考の単語集 (core/obs/glossary.py), keeping its meaning
  and adding nothing.
- 申し送りの下書き (handoverDraft): in the handover memo
  (core/obs/handover.py), a few sentences for the next shift made from the
  lists the memo was made of.
- For a model of about 3B (tsudo 2026-11-19: 「3Bに対応しつつ、コンフィグで
  できる範囲を規定」「モデル一覧のコンフィグと、やることに対してどのモデルを
  はめるか…はめがない場合機能しない」; core/text_tasks.py): 項目の提案
  (noteFields: the record's fields picked from its 備考), 言葉で探す
  (searchWords: the export's conditions from a sentence), まとめ (summary: a
  few sentences on records) and 英訳 (translate: the 備考 in English for the
  通報's `note.en`).
- 依頼文から下書き (requestDraft; core/text_request.py, tsudo 2026-11-21): a
  request's mail or chat text made into the request form's fields (new or
  edited), the dates, numbers, satellites and choice words read by code.

A small model writes well-formed sentences but can get facts wrong, so what
it writes is checked here: numbers that are not in what it was given are
pointed out, the glossary's spellings are put back, values not among the
choices are dropped, and nothing is saved without the person choosing it.

The settings file's `textAssist` lists the models by name (a folder or .zip
of an ONNX Runtime GenAI model: genai_config.json, model.onnx,
tokenizer.json …) and gives each task one of them; a task given none does
not show. The person using the plugin never chooses a model. The
instructions given to the model may be changed per task.

This module is pure Python: the prompts, the settings and the checks. The
model runs in qgis_io/text_assist_io.py.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Sequence

from .util import is_number, is_record

#: The instructions for 備考を整える (the task's `prompt` replaces them).
NOTE_PROMPT = (
    "あなたは衛星画像の判読記録の備考を整える係です。"
    "与えられた備考を、意味を変えず、情報を足さず、短い判読記録の文体（体言止め。です・ます調にしない）に書き直してください。"
    "備考にないこと（様子・理由・推測）は書き足さないでください。"
    "誤字・脱字を直し、数字・固有名詞・記号はそのまま残してください。"
    "「表記」があれば、備考にある語はその表記で書いてください（備考にない語は足さない）。"
    "出力は書き直した備考の 1 行だけにしてください。"
)
#: Examples shown to the model before the 備考 (input → what it should write); the task's `examples` replace them.
NOTE_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("種別: 船舶 > 駆逐艦\n備考: 岸壁に停泊してるみたい。たぶん2隻くらいいると思います", "岸壁に停泊中。2 隻程度と推定。"),
    ("種別: 車両\n備考: ゲートの前にトラックがならんでる、5台", "ゲート前にトラック 5 台が並ぶ。"),
)
#: The instructions for 申し送りの下書き (the task's `prompt` replaces them).
HANDOVER_PROMPT = (
    "あなたは衛星画像の判読業務の引き継ぎを手伝います。"
    "下の引き継ぎメモ（自動でまとめた一覧）から、次の担当者への申し送りを 2〜4 文で書いてください。"
    "書くのは、期限が近い撮像依頼、担当中で終わっていない画像と残っている作業、通報したもの、の順に 1 文ずつです（メモにないものは書かない）。"
    "件数の一覧は繰り返さないでください。メモにない事実・数字・推測・助言（「〜してください」）は書かないでください。"
    "宛名・あいさつ・結びの言葉や箇条書きの記号は使わず、申し送りの文章だけを出力してください。"
)
#: The instructions for 項目の提案 (core/text_tasks.py).
FIELDS_PROMPT = (
    "あなたは衛星画像の判読記録の入力を手伝います。"
    "備考を読み、記録の項目に入れられる値を拾ってください。"
    "種別・活動・確度は、示した候補の中から候補の書き方のまま選んでください（合うものがなければ「なし」）。"
    "確度は備考に「推定」「たぶん」「確実」などと書いてあるときだけです。"
    "長さ・幅・向きは、備考に書いてある数字（長さ・幅は m、向きは度）だけを使ってください（なければ「なし」）。"
    "モノは、備考に書いてある固有の名前（艦名・機番など）だけです。「補給艦」「戦闘機」のような種類の名前は「なし」です。"
    "推測で値を作らないでください。値だけを書き、説明・理由・括弧書きは書かないでください。出力は次の 7 行だけにしてください。\n"
    "種別: \n活動: \n確度: \n長さ: \n幅: \n向き: \nモノ: "
)
#: The instructions for 言葉で探す.
SEARCH_PROMPT = (
    "あなたは観測記録の検索条件を作る係です。"
    "依頼の文から、期間・地名・大分類・確度・有無の条件を読み取ってください。"
    "期間は「日付の目安」の日付を使い、「YYYY-MM-DD 〜 YYYY-MM-DD」の形で書いてください（片方だけなら空けてよい）。"
    "地名・大分類・確度は、示した候補の中から選んでください。"
    "有無は「あり」（いた記録）「消失」（いなくなった記録）「不可」（雲などで見えなかった記録）のどれかです。"
    "依頼にない条件は「なし」と書いてください。値だけを書き、説明・理由・括弧書きは書かないでください。"
    "出力は次の 5 行だけにしてください。\n"
    "期間: \n地名: \n大分類: \n確度: \n有無: "
)
#: The instructions for まとめ.
SUMMARY_PROMPT = (
    "あなたは衛星画像の判読結果をまとめる係です。"
    "下の観測記録（1 行 1 件）を読み、何がどこにどれだけあり、何をしていたかを 3〜6 文の文章にまとめてください。"
    "多いもの・目立つ動きを先に書き、同じものはまとめてください。"
    "記録にない事実や数字、推測・評価・意図の解釈は書かないでください（記録に書いてあることだけ）。"
    "箇条書きにせず、まとめの文章だけを出力してください。"
)
#: The instructions for 英訳.
TRANSLATE_PROMPT = (
    "You translate notes written by Japanese satellite image analysts into English for a report. "
    "Translate the note faithfully and concisely in the style of an analyst's note. "
    "Keep numbers, names and symbols as they are, and add nothing. Use the given terms for the words they name. "
    "Write every word in English (no Japanese left). Output only the English translation on one line, without quotes."
)
#: The instructions for 依頼文から下書き (core/text_request.py).
REQUEST_PROMPT = (
    "あなたは衛星画像の撮像依頼を受け付ける係です。"
    "依頼の文を読み、撮像依頼の項目を書き出してください。"
    "名称は、地名と目的が分かる 20 文字ほどの短い件名にしてください。"
    "地名は、文に書かれた地名（港・基地・空港・施設など）をそのまま写し、複数あれば「、」で区切ってください。"
    "期間は「日付の目安」の日付を使い、「YYYY-MM-DD 〜 YYYY-MM-DD」の形で書いてください（片方だけなら空けてよい）。"
    "頻度は「期間中に1回」「毎日」「曜日:月・木」「n日ごと」のどれかです。"
    "優先度は 最優先・高・中・低・最低 のどれかです。"
    "雲量・分解能・オフナディア角・入射角は、文の数字と「以下」「以上」などの言葉をそのまま写してください。"
    "時間帯は 昼・夜 のどちらかです。1日の回数は数字だけです。依頼者は文に書かれた名前や部署を写してください。"
    "文にない項目は「なし」と書いてください。推測で値を作らないでください。"
    "値だけを書き、説明・理由・括弧書きは書かないでください。出力は次の 12 行だけにしてください。\n"
    "名称: \n地名: \n期間: \n頻度: \n優先度: \n雲量: \n分解能: \nオフナディア角: \n入射角: \n時間帯: \n1日の回数: \n依頼者: "
)
#: The most a 備考 sent to the model may be (characters; longer ones are not tidied).
NOTE_LIMIT = 400
#: The most of the memo sent to the model (characters; the rest is cut, the lists come first).
HANDOVER_LIMIT = 3000
#: The most words of the glossary named to the model with a 備考.
TERMS_LIMIT = 30

#: What the model can be asked to do (`textAssist.tasks`), in the order they are listed.
TASKS = ("tidyNote", "handoverDraft", "noteFields", "searchWords", "summary", "translate", "requestDraft")
TASK_LABELS = {"tidyNote": "備考を整える", "handoverDraft": "申し送りの下書き", "noteFields": "項目の提案", "searchWords": "言葉で探す",
               "summary": "まとめ", "translate": "英訳", "requestDraft": "依頼文から下書き"}
#: The tasks a model of about 3B is meant for: a smaller one is said to be (not refused).
LARGE_TASKS = frozenset({"noteFields", "searchWords", "summary", "translate", "requestDraft"})
#: Smaller than this (billions of parameters) is a small model for LARGE_TASKS.
LARGE_MODEL = 2.0
#: Each task's own: how long it may write (tokens) and its instructions.
TASK_DEFAULTS: dict[str, tuple[int, str]] = {
    "tidyNote": (160, NOTE_PROMPT), "handoverDraft": (320, HANDOVER_PROMPT), "noteFields": (160, FIELDS_PROMPT),
    "searchWords": (160, SEARCH_PROMPT), "summary": (480, SUMMARY_PROMPT), "translate": (240, TRANSLATE_PROMPT),
    "requestDraft": (240, REQUEST_PROMPT),
}
#: The name of the one model of the shorter settings (`"textAssist": "…"`, or `{ "model": … }`).
ONE_MODEL = "model"


@dataclass(frozen=True)
class TextModel:
    """One of `textAssist.models`."""
    name: str
    #: A folder (or .zip of one) holding genai_config.json, as written (relative to the settings file), or a .zip's URL.
    path: str
    #: Threads it uses (0: all cores but one).
    threads: int = 0


@dataclass(frozen=True)
class TextTask:
    """One of `textAssist.tasks`: the model it is given to, how long it may write and what it is told."""
    name: str
    model: str
    max_tokens: int
    prompt: str
    #: Examples shown before what is asked (input, what it should write); 備考を整える and 依頼文から下書き only.
    examples: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        return TASK_LABELS.get(self.name, self.name)


def task_of(name: str, model: str, max_tokens: Optional[int] = None, prompt: Optional[str] = None,
            examples: Optional[tuple[tuple[str, str], ...]] = None) -> TextTask:
    """A task with its defaults where nothing else is given."""
    tokens, text = TASK_DEFAULTS[name]
    if examples is None:
        examples = NOTE_EXAMPLES if name == "tidyNote" else ()
    return TextTask(name, model, max_tokens or tokens, prompt or text, examples)


@dataclass(frozen=True)
class TextAssistSettings:
    """
    `textAssist`: the models (by name) and which one each task is given to.
    A task given no model is not offered (its button does not show). One
    model is loaded at a time.
    """
    models: tuple[TextModel, ...]
    tasks: tuple[TextTask, ...]
    #: Minutes a model stays loaded after its last use (memory is given back after).
    keep_minutes: float = 10.0

    def task(self, name: str) -> Optional[TextTask]:
        return next((t for t in self.tasks if t.name == name), None)

    def model_for(self, name: str) -> Optional[TextModel]:
        """The model the task `name` is given to (None: not offered)."""
        t = self.task(name)
        return next((m for m in self.models if t is not None and m.name == t.model), None)

    def has(self, name: str) -> bool:
        return self.model_for(name) is not None


def _int_of(value: Any, at: str, low: int, high: int, problems: list[str]) -> Optional[int]:
    if is_number(value) and not isinstance(value, bool) and float(value).is_integer() and low <= value <= high:
        return int(value)
    problems.append(f"{at} は {low}〜{high} の整数で指定")
    return None


def _text_of(value: Any, at: str, problems: list[str]) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    problems.append(f"{at} はモデルへの指示の文章で指定")
    return None


def _keep_minutes(value: dict, at: str, problems: list[str]) -> float:
    if "keepMinutes" not in value:
        return 10.0
    v = value["keepMinutes"]
    if is_number(v) and not isinstance(v, bool) and 0 <= v <= 1440:
        return float(v)
    problems.append(f"{at}.keepMinutes は使い終えてからモデルを残しておく分数（0〜1440、0 ですぐ外す）で指定")
    return 10.0


def _unknown(value: dict, known: Sequence[str], at: str, problems: list[str]) -> None:
    unknown = sorted(set(value) - set(known))
    if unknown:
        problems.append(f"{at} の {'・'.join(unknown)} は使わない（綴りの確認）")


def text_assist_settings_of(value: Any, problems: list[str]) -> Optional[TextAssistSettings]:
    """
    `textAssist`: `{ models: { 名前: 場所 | { path, threads } }, tasks: { タスク: 名前 | { model, maxTokens, prompt,
    examples } }, keepMinutes }`. The shorter one model (a string, or `{ model, threads, maxTokens, handoverMaxTokens,
    keepMinutes, notePrompt, noteExamples, handoverPrompt }`) is that model given 備考を整える and 申し送りの下書き.
    """
    at = "textAssist"
    if isinstance(value, str):
        value = {"model": value}
    if not is_record(value):
        problems.append(f"{at} は {{ models, tasks }}（モデルの一覧とタスクごとの割り当て）か、モデルの場所（文字列）で指定")
        return None
    if "models" in value or "tasks" in value:
        if "model" in value:
            problems.append(f"{at} の model と models は一緒に使わない（models に名前を付けて書き、tasks で割り当てる）")
        return _settings_of(value, at, problems)
    return _one_model_of(value, at, problems)


def _one_model_of(value: dict, at: str, problems: list[str]) -> Optional[TextAssistSettings]:
    """The shorter settings: one model, given 備考を整える and 申し送りの下書き (the first settings, 2026-11-19)."""
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        problems.append(f"{at}.model にモデルのフォルダ（genai_config.json のあるフォルダ）か .zip を指定"
                        "（モデルを使い分けるなら models と tasks）")
        return None
    threads = _int_of(value["threads"], f"{at}.threads", 0, 64, problems) if "threads" in value else None
    note_tokens = _int_of(value["maxTokens"], f"{at}.maxTokens", 16, 2048, problems) if "maxTokens" in value else None
    handover_tokens = (_int_of(value["handoverMaxTokens"], f"{at}.handoverMaxTokens", 16, 4096, problems)
                       if "handoverMaxTokens" in value else None)
    note_prompt = _text_of(value["notePrompt"], f"{at}.notePrompt", problems) if "notePrompt" in value else None
    handover_prompt = _text_of(value["handoverPrompt"], f"{at}.handoverPrompt", problems) if "handoverPrompt" in value else None
    examples = _examples_of(value["noteExamples"], f"{at}.noteExamples", problems) if "noteExamples" in value else None
    _unknown(value, ("model", "threads", "maxTokens", "handoverMaxTokens", "keepMinutes", "notePrompt", "noteExamples",
                     "handoverPrompt"), at, problems)
    return TextAssistSettings(
        (TextModel(ONE_MODEL, model.strip(), threads or 0),),
        (task_of("tidyNote", ONE_MODEL, note_tokens, note_prompt, examples), task_of("handoverDraft", ONE_MODEL, handover_tokens, handover_prompt)),
        _keep_minutes(value, at, problems))


def _settings_of(value: dict, at: str, problems: list[str]) -> Optional[TextAssistSettings]:
    models: list[TextModel] = []
    given = value.get("models", {})
    if not is_record(given) or not given:
        problems.append(f"{at}.models はモデルの一覧 {{ \"名前\": \"場所\" か {{ \"path\", \"threads\" }} }} で指定")
        given = {}
    for name, m in given.items():
        where = f"{at}.models.{name}"
        if not str(name).strip():
            problems.append(f"{at}.models の名前が空")
            continue
        if isinstance(m, str):
            m = {"path": m}
        if not is_record(m) or not isinstance(m.get("path"), str) or not m["path"].strip():
            problems.append(f"{where} はモデルの場所（genai_config.json のあるフォルダ・.zip・.zip の URL）か {{ path, threads }} で指定")
            continue
        threads = _int_of(m["threads"], f"{where}.threads", 0, 64, problems) if "threads" in m else None
        _unknown(m, ("path", "threads"), where, problems)
        models.append(TextModel(str(name).strip(), m["path"].strip(), threads or 0))
    names = {m.name for m in models}
    tasks: list[TextTask] = []
    given = value.get("tasks", {})
    if not is_record(given):
        problems.append(f"{at}.tasks はタスクごとのモデル {{ \"tidyNote\": \"モデルの名前\", … }} で指定")
        given = {}
    if not given:
        problems.append(f"{at}.tasks にモデルを割り当てたタスクがない（文章の手伝いは何も出ない。"
                        f"タスク: {'・'.join(f'{t}（{TASK_LABELS[t]}）' for t in TASKS)}）")
    for name in sorted(given, key=lambda n: TASKS.index(n) if n in TASKS else len(TASKS)):
        t = given[name]
        where = f"{at}.tasks.{name}"
        if name not in TASKS:
            problems.append(f"{where} は使わない（タスク: {'・'.join(TASKS)}）")
            continue
        if isinstance(t, str):
            t = {"model": t}
        if not is_record(t) or not isinstance(t.get("model"), str):
            problems.append(f"{where} は models の名前か {{ model, maxTokens, prompt }} で指定")
            continue
        model = t["model"].strip()
        if model not in names:
            problems.append(f"{where} のモデル「{model}」は models にない（{'・'.join(sorted(names)) or 'models が空'}）。"
                            f"{TASK_LABELS[name]}は出ない")
            continue
        tokens = _int_of(t["maxTokens"], f"{where}.maxTokens", 16, 4096, problems) if "maxTokens" in t else None
        prompt = _text_of(t["prompt"], f"{where}.prompt", problems) if "prompt" in t else None
        examples = None
        known = ["model", "maxTokens", "prompt"]
        if name == "tidyNote":
            known.append("examples")
            examples = _examples_of(t["examples"], f"{where}.examples", problems) if "examples" in t else None
        elif name == "requestDraft":
            known.append("examples")
            examples = _request_examples_of(t["examples"], f"{where}.examples", problems) if "examples" in t else None
        _unknown(t, known, where, problems)
        tasks.append(task_of(name, model, tokens, prompt, examples))
    _unknown(value, ("models", "tasks", "keepMinutes", "model"), at, problems)
    return TextAssistSettings(tuple(models), tuple(tasks), _keep_minutes(value, at, problems))


def _examples_of(value: Any, at: str, problems: list[str]) -> Optional[tuple[tuple[str, str], ...]]:
    if not isinstance(value, list):
        problems.append(f"{at} は [{{ \"note\": 元の備考, \"tidy\": 整えた備考, \"kind\": 種別（任意） }}] の配列で指定")
        return None
    out = []
    for i, e in enumerate(value):
        if not is_record(e) or not isinstance(e.get("note"), str) or not isinstance(e.get("tidy"), str):
            problems.append(f"{at}[{i}] は {{ \"note\", \"tidy\", \"kind\" }} で指定")
            return None
        kind = e.get("kind") if isinstance(e.get("kind"), str) else ""
        out.append((_note_input(e["note"], kind), e["tidy"].strip()))
    return tuple(out)


def _request_examples_of(value: Any, at: str, problems: list[str]) -> Optional[tuple[tuple[str, str], ...]]:
    """依頼文から下書き's examples: [{ "text": 依頼の文, "answer": 「名称: …」の 12 行 }]."""
    if not isinstance(value, list):
        problems.append(f"{at} は [{{ \"text\": 依頼の文, \"answer\": 答えの行（名称: … 依頼者: …） }}] の配列で指定")
        return None
    out = []
    for i, e in enumerate(value):
        if not is_record(e) or not isinstance(e.get("text"), str) or not isinstance(e.get("answer"), str):
            problems.append(f"{at}[{i}] は {{ \"text\", \"answer\" }} で指定")
            return None
        out.append((e["text"].strip(), e["answer"].strip()))
    return tuple(out)


# ---- what the model is asked ---------------------------------------------------------------

Message = dict[str, str]


def _note_input(note: str, kind: str = "", terms: Sequence[str] = ()) -> str:
    lines = []
    if kind.strip():
        lines.append(f"種別: {kind.strip()}")
    if terms:
        lines.append("表記: " + "、".join(terms))
    lines.append(f"備考: {note.strip()}")
    return "\n".join(lines)


def note_terms(glossary, note: str, root: str = "", limit: int = TERMS_LIMIT) -> list[str]:
    """
    The glossary's words named to the model with `note`: those whose other
    spellings are in it first (so it writes them as the glossary does), then
    those it already uses, up to `limit`. Not the 大分類's other words: a
    small model given 「航跡」 writes 「航跡あり」 into a 備考 that did not
    say so (tried on sarashina2.2 1B and 3B, 2026-11-20).
    """
    if not glossary:
        return []
    out: list[str] = [v.term for v in glossary.variants_in(note, root)]
    folded = unicodedata.normalize("NFKC", note).lower()
    out += [t.text for t in glossary.terms if t.for_kind(root) and unicodedata.normalize("NFKC", t.text).lower() in folded]
    return list(dict.fromkeys(out))[:limit]


def note_messages(task: TextTask, note: str, kind: str = "", terms: Sequence[str] = ()) -> list[Message]:
    """The conversation that asks the model to tidy `note` (the examples as earlier turns)."""
    messages: list[Message] = [{"role": "system", "content": task.prompt}]
    for given, wanted in task.examples:
        messages += [{"role": "user", "content": given}, {"role": "assistant", "content": wanted}]
    messages.append({"role": "user", "content": _note_input(note[:NOTE_LIMIT], kind, terms)})
    return messages


#: The memo's own 申し送り section (written by hand) and what follows it.
_MEMO_SECTION = re.compile(r"(?m)^■ 申し送り.*\Z", re.DOTALL)


def handover_source(memo: str, limit: int = HANDOVER_LIMIT) -> str:
    """The memo's lists given to the model: without its 申し送り section, cut to `limit` characters."""
    text = _MEMO_SECTION.sub("", memo.replace("\r\n", "\n")).strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…（以下略）"
    return text


def handover_messages(task: TextTask, memo: str, written: str = "") -> list[Message]:
    """The conversation that asks for a 申し送り from the memo `memo` (and what the person wrote so far)."""
    content = "引き継ぎメモ:\n" + handover_source(memo)
    if written.strip():
        content += "\n\n書きかけの申し送り（生かして書く）:\n" + written.strip()
    return [{"role": "system", "content": task.prompt}, {"role": "user", "content": content}]


def plain_prompt(messages: Sequence[Message]) -> str:
    """The conversation as plain text, for a model without a chat template."""
    out = []
    for m in messages:
        label = {"system": "指示", "user": "入力", "assistant": "出力"}.get(m["role"], m["role"])
        out.append(f"### {label}\n{m['content']}")
    return "\n\n".join(out) + "\n\n### 出力\n"


# ---- what it wrote ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Draft:
    """What the model wrote, cleaned, and what to look at before taking it."""
    text: str
    #: Numbers in it that are not in what it was given (the model may have made them up).
    foreign: tuple[str, ...] = ()
    #: Much longer than what it was given (a 備考: it may have added things).
    longer: bool = False
    #: The same as what it was given (nothing to change).
    same: bool = False
    #: Other things to look at (values left out, a small model for the task).
    notes: tuple[str, ...] = ()

    @property
    def warnings(self) -> list[str]:
        out = list(self.notes)
        if self.foreign:
            out.append(f"元にない数字: {'・'.join(self.foreign)}")
        if self.longer:
            out.append("元より大幅に長い（足された内容がないか確認）")
        return out


_LABEL = re.compile(r"^\s*(?:#+\s*)?(?:出力|備考|整えた備考|書き直した備考|申し送り|まとめ|要約|案|回答)\s*[:：]\s*")
_QUOTES = (("「", "」"), ("『", "』"), ('"', '"'), ("“", "”"), ("'", "'"), ("`", "`"))


def _unquoted(text: str) -> str:
    for a, b in _QUOTES:
        once = text.count(a) == 2 if a == b else text.count(a) == 1 and text.count(b) == 1
        if len(text) > len(a) + len(b) and text.startswith(a) and text.endswith(b) and once:
            return text[len(a):-len(b)].strip()
    return text


def clean_note(raw: str) -> str:
    """The model's 備考: its first line with words, without a label (「備考:」) or quotes around it."""
    lines = [re.sub(r"^```\w*|```$", "", line).strip() for line in raw.replace("\r\n", "\n").split("\n")]
    line = next((x for x in lines if x and not x.startswith("###")), "")
    line = _unquoted(_LABEL.sub("", line))
    return re.sub(r"[ \t　]+", lambda m: " " if m[0].isascii() else m[0][0], line).strip()


#: A line that only addresses or introduces (「引き継ぎ担当者様、」「申し送りは以下の通りです。」).
_GREETING = re.compile(r"^(?:[^。]{0,24}(?<!模)(?:様|殿|各位|さんへ|様へ)[、,。]?|お疲れ様(?:です)?[。、]?"
                       r"|.{0,60}(?:以下の(?:通り|とおり)|次の(?:通り|とおり)|をまとめ(?:ます|ました)).{0,6})$")
#: A closing paragraph (「以上、…」「ご不明な点は…」).
_CLOSING = re.compile(r"^以上[、。]|ご不明な点|お問い合わせ|お気軽に|よろしくお願い")


_BULLET = re.compile(r"^\s*(?:[・•\-*]|[①-⑳]|\(?\d{1,2}[.)）、])\s*")


def without_letter(text: str) -> str:
    """`text` without a leading address or introduction line and without closing paragraphs (a small model writes a letter)."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    while paragraphs:
        lines = paragraphs[0].split("\n")
        while lines and _GREETING.match(lines[0].strip()):
            lines.pop(0)
        if lines:
            paragraphs[0] = "\n".join(lines).strip()
            break
        paragraphs.pop(0)
    while len(paragraphs) > 1 and _CLOSING.search(paragraphs[-1].strip()):
        paragraphs.pop()
    return "\n\n".join(paragraphs).strip()


def clean_paragraph(raw: str) -> str:
    """The model's 申し送り or まとめ: its text without a label, code fences, a heading, an address or a closing; lines kept."""
    text = re.sub(r"```\w*", "", raw.replace("\r\n", "\n"))
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("###")):
        lines.pop(0)
    if lines:
        lines[0] = _LABEL.sub("", lines[0])
    # A list written in spite of being asked not to: its marks taken off (its lines kept).
    lines = [_BULLET.sub("", line) for line in lines]
    text = without_letter("\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", text)


_SENTENCES = re.compile(r"(\d+)\s*[〜~～ー-]\s*(\d+)\s*文")
_SENTENCE_END = re.compile(r"[。！？]")


def sentence_limit(prompt: str) -> Optional[int]:
    """The most sentences an instruction asks for (「2〜4 文で」 → 4); None when it does not say."""
    m = _SENTENCES.search(unicodedata.normalize("NFKC", prompt).replace("~", "〜"))
    return int(m[2]) if m else None


def sentences_done(most: Optional[int]) -> Optional[Callable[[str], bool]]:
    """
    Whether a paragraph has its `most` sentences (an address or
    introduction not counted): the model stops there instead of going on
    (a small model writes on until its token limit).
    """
    if not most:
        return None

    def done(text: str) -> bool:
        body = without_letter(re.sub(r"```\w*", "", text))
        return len(_SENTENCE_END.findall(body)) >= most

    return done


_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}
_KANJI_NUMBER = re.compile(r"[〇零一二三四五六七八九十百千]+")


def _kanji_value(text: str) -> Optional[int]:
    """「二十五」 as 25 (up to 9999); None when it is not a plain number."""
    if all(c in _KANJI_DIGITS for c in text):
        return int("".join(str(_KANJI_DIGITS[c]) for c in text)) if len(text) > 1 or text != "〇" else 0
    total, digit = 0, None
    for c in text:
        if c in _KANJI_DIGITS:
            if digit is not None:
                return None
            digit = _KANJI_DIGITS[c]
        else:
            total += (1 if digit is None else digit) * _KANJI_UNITS[c]
            digit = None
    return total + (digit or 0)


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> list[str]:
    """The numbers written in `text` (full-width digits as half-width, 「1,200」 as 1200, kanji numbers as digits)."""
    t = unicodedata.normalize("NFKC", text)
    out = [m[0].replace(",", "") for m in _NUMBER.finditer(t)]
    for m in _KANJI_NUMBER.finditer(t):
        v = _kanji_value(m[0])
        if v is not None:
            out.append(str(v))
    return out


def _same_number(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def foreign_numbers(given: str, written: str) -> tuple[str, ...]:
    """The numbers in `written` that `given` does not have."""
    have = numbers_in(given)
    return tuple(dict.fromkeys(n for n in numbers_in(written) if not any(_same_number(n, h) for h in have)))


def _key(text: str) -> str:
    return re.sub(r"[\s　、。，．,.]", "", unicodedata.normalize("NFKC", text))


#: A sentence on what was done (「表記を統一し、簡潔に書き直しました。」), written after the 備考 by a small model.
_DONE = re.compile(r"(?<=。)[^。]*(?:書き直し|統一し|修正し|整えました|直しました|まとめました)[^。]*。?$")


def note_draft(raw: str, note: str, glossary=None, root: str = "") -> Draft:
    """The model's tidied 備考 (without a sentence on what it did), written with the glossary's words, and what to check in it."""
    text = _DONE.sub("", clean_note(raw)).strip()
    if glossary:
        text = glossary.fixed(text, root)
    longer = len(text) > max(2 * len(note.strip()), len(note.strip()) + 40)
    return Draft(text, foreign_numbers(note, text), longer, _key(text) == _key(note))


def handover_draft(raw: str, memo: str, written: str = "") -> Draft:
    """The model's 申し送り, and the numbers in it that the memo (or what was written) does not have."""
    text = clean_paragraph(raw)
    return Draft(text, foreign_numbers(handover_source(memo) + "\n" + written, text), False, False)


# ---- the model -----------------------------------------------------------------------------------

class Writer(Protocol):
    """A model that continues a conversation (qgis_io/text_assist_io.GenAiWriter; tests give their own)."""

    def write(self, messages: Sequence[Message], max_tokens: int, cancelled: Callable[[], bool],
              wrote: Callable[[str], None], one_line: bool = False, until: Optional[Callable[[str], bool]] = None) -> str:
        """
        The reply to `messages` (at most `max_tokens`), `wrote` called with
        the text so far as it grows; `one_line`: its first line; `until`:
        ends once it says the text so far is enough.
        """
        ...

