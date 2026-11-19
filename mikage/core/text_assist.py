"""
文章の手伝い（小さな生成 AI）: a small generative model run on this computer
(tsudo 2026-11-19: 「本当に小さい機能でいいので、量子化されたミニ生成AI機能
みたいなの同梱できないか検討してみて」 → 「両方（備考を先に）」, model
「sarashina2.2-1Bで良いよ」).

Two small uses, both only proposals that a person reads and takes or leaves:

- 備考を整える: a 備考 typed in the observation record (or checked area)
  form, written again in the plain style of a reading record, with the words
  of the 備考の単語集 (core/obs/glossary.py), keeping its meaning and adding
  nothing.
- 申し送りの下書き: in the handover memo (core/obs/handover.py), a few
  sentences for the next shift made from the lists the memo was made of.

A small model writes well-formed sentences but can get facts wrong, so what
it writes is checked here: numbers that are not in what it was given are
pointed out, the glossary's spellings are put back, and the result is never
saved without the person choosing it.

The settings file's `textAssist` names the model (a folder or .zip of an
ONNX Runtime GenAI model: genai_config.json, model.onnx, tokenizer.json …),
how many threads it uses and how long it may write; the instructions given
to the model may be changed there too. Without it nothing shows.

This module is pure Python: the prompts, the settings and the checks. The
model runs in qgis_io/text_assist_io.py.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Sequence

from .util import is_number, is_record

#: The instructions for 備考を整える (the settings' `notePrompt` replaces them).
NOTE_PROMPT = (
    "あなたは衛星画像の判読記録の備考を整える係です。"
    "与えられた備考を、意味を変えず、情報を足さず、短い判読記録の文体（体言止め。です・ます調にしない）に書き直してください。"
    "誤字・脱字を直し、数字・固有名詞・記号はそのまま残してください。"
    "「使う語」があれば、その表記を使ってください。"
    "出力は書き直した備考の 1 行だけにしてください。"
)
#: Examples shown to the model before the 備考 (input → what it should write); `noteExamples` replaces them.
NOTE_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("種別: 船舶 > 駆逐艦\n備考: 岸壁に停泊してるみたい。たぶん2隻くらいいると思います", "岸壁に停泊中。2 隻程度と推定。"),
    ("種別: 車両\n備考: ゲートの前にトラックがならんでる、5台", "ゲート前にトラック 5 台が並ぶ。"),
)
#: The instructions for 申し送りの下書き (`handoverPrompt` replaces them).
HANDOVER_PROMPT = (
    "あなたは衛星画像の判読業務の引き継ぎを手伝います。"
    "下の引き継ぎメモ（自動でまとめた一覧）を読み、次の担当者への申し送りを 2〜4 文で下書きしてください。"
    "期限が近い撮像依頼、担当中で終わっていない画像、通報したものを先に書いてください。"
    "メモにない事実や数字は書かないでください。箇条書きにせず、文章だけを出力してください。"
)
#: The most a 備考 sent to the model may be (characters; longer ones are not tidied).
NOTE_LIMIT = 400
#: The most of the memo sent to the model (characters; the rest is cut, the lists come first).
HANDOVER_LIMIT = 3000
#: The most words of the glossary named to the model with a 備考.
TERMS_LIMIT = 30


@dataclass(frozen=True)
class TextAssistSettings:
    """`textAssist`."""
    #: The model: a folder (or .zip of one) holding genai_config.json, as written (relative to the settings file).
    model: str
    #: Threads the model uses (0: all cores but one).
    threads: int = 0
    #: How long a tidied 備考 may be (tokens the model writes).
    max_tokens: int = 160
    #: How long a 申し送り draft may be (tokens).
    handover_max_tokens: int = 320
    #: Minutes the model stays loaded after its last use (memory is given back after).
    keep_minutes: float = 10.0
    note_prompt: str = NOTE_PROMPT
    note_examples: tuple[tuple[str, str], ...] = NOTE_EXAMPLES
    handover_prompt: str = HANDOVER_PROMPT


def _int_of(value: Any, at: str, low: int, high: int, problems: list[str]) -> Optional[int]:
    if is_number(value) and not isinstance(value, bool) and float(value).is_integer() and low <= value <= high:
        return int(value)
    problems.append(f"{at} は {low}〜{high} の整数で指定")
    return None


def text_assist_settings_of(value: Any, problems: list[str]) -> Optional[TextAssistSettings]:
    """`textAssist`: the model's place (a string), or `{ model, threads, maxTokens, handoverMaxTokens, keepMinutes, notePrompt, noteExamples, handoverPrompt }`."""
    at = "textAssist"
    if isinstance(value, str):
        value = {"model": value}
    if not is_record(value):
        problems.append(f"{at} はモデルの場所（文字列）か {{ model, threads, maxTokens, … }} で指定")
        return None
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        problems.append(f"{at}.model にモデルのフォルダ（genai_config.json のあるフォルダ）か .zip を指定")
        return None
    kept: dict[str, Any] = {"model": model.strip()}
    for key, name, low, high in (("threads", "threads", 0, 64), ("maxTokens", "max_tokens", 16, 2048),
                                 ("handoverMaxTokens", "handover_max_tokens", 16, 4096)):
        if key in value:
            n = _int_of(value[key], f"{at}.{key}", low, high, problems)
            if n is not None:
                kept[name] = n
    if "keepMinutes" in value:
        v = value["keepMinutes"]
        if is_number(v) and not isinstance(v, bool) and 0 <= v <= 1440:
            kept["keep_minutes"] = float(v)
        else:
            problems.append(f"{at}.keepMinutes は使い終えてからモデルを残しておく分数（0〜1440、0 ですぐ外す）で指定")
    for key, name in (("notePrompt", "note_prompt"), ("handoverPrompt", "handover_prompt")):
        if key in value:
            v = value[key]
            if isinstance(v, str) and v.strip():
                kept[name] = v.strip()
            else:
                problems.append(f"{at}.{key} はモデルへの指示の文章で指定")
    if "noteExamples" in value:
        examples = _examples_of(value["noteExamples"], f"{at}.noteExamples", problems)
        if examples is not None:
            kept["note_examples"] = examples
    unknown = sorted(set(value) - {"model", "threads", "maxTokens", "handoverMaxTokens", "keepMinutes", "notePrompt",
                                   "noteExamples", "handoverPrompt"})
    if unknown:
        problems.append(f"{at} の {'・'.join(unknown)} は使わない（綴りの確認）")
    return TextAssistSettings(**kept)


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


# ---- what the model is asked ---------------------------------------------------------------

Message = dict[str, str]


def _note_input(note: str, kind: str = "", terms: Sequence[str] = ()) -> str:
    lines = []
    if kind.strip():
        lines.append(f"種別: {kind.strip()}")
    if terms:
        lines.append("使う語: " + "、".join(terms))
    lines.append(f"備考: {note.strip()}")
    return "\n".join(lines)


def note_terms(glossary, note: str, root: str = "", limit: int = TERMS_LIMIT) -> list[str]:
    """
    The glossary's words named to the model with `note`: those whose other
    spellings are in it first (so it writes them as the glossary does), then
    those it already uses, then the 大分類's own words, up to `limit`.
    """
    if not glossary:
        return []
    out: list[str] = [v.term for v in glossary.variants_in(note, root)]
    folded = unicodedata.normalize("NFKC", note).lower()
    out += [t.text for t in glossary.terms if t.for_kind(root) and unicodedata.normalize("NFKC", t.text).lower() in folded]
    if root:
        out += [t.text for t in glossary._ordered(t for t in glossary.terms if root in t.kinds)]
    return list(dict.fromkeys(out))[:limit]


def note_messages(settings: TextAssistSettings, note: str, kind: str = "", terms: Sequence[str] = ()) -> list[Message]:
    """The conversation that asks the model to tidy `note` (the examples as earlier turns)."""
    messages: list[Message] = [{"role": "system", "content": settings.note_prompt}]
    for given, wanted in settings.note_examples:
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


def handover_messages(settings: TextAssistSettings, memo: str, written: str = "") -> list[Message]:
    """The conversation that asks for a 申し送り from the memo `memo` (and what the person wrote so far)."""
    content = "引き継ぎメモ:\n" + handover_source(memo)
    if written.strip():
        content += "\n\n書きかけの申し送り（生かして書く）:\n" + written.strip()
    return [{"role": "system", "content": settings.handover_prompt}, {"role": "user", "content": content}]


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

    @property
    def warnings(self) -> list[str]:
        out = []
        if self.foreign:
            out.append(f"元にない数字: {'・'.join(self.foreign)}")
        if self.longer:
            out.append("元より大幅に長い（足された内容がないか確認）")
        return out


_LABEL = re.compile(r"^\s*(?:#+\s*)?(?:出力|備考|整えた備考|書き直した備考|申し送り|案|回答)\s*[:：]\s*")
_QUOTES = (("「", "」"), ("『", "』"), ('"', '"'), ("“", "”"), ("'", "'"), ("`", "`"))


def _unquoted(text: str) -> str:
    for a, b in _QUOTES:
        if len(text) > len(a) + len(b) and text.startswith(a) and text.endswith(b) and text.count(a) == 1 and text.count(b) == 1:
            return text[len(a):-len(b)].strip()
    return text


def clean_note(raw: str) -> str:
    """The model's 備考: its first line with words, without a label (「備考:」) or quotes around it."""
    lines = [re.sub(r"^```\w*|```$", "", line).strip() for line in raw.replace("\r\n", "\n").split("\n")]
    line = next((x for x in lines if x and not x.startswith("###")), "")
    line = _unquoted(_LABEL.sub("", line))
    return re.sub(r"[ \t　]+", lambda m: " " if m[0].isascii() else m[0][0], line).strip()


def clean_paragraph(raw: str) -> str:
    """The model's 申し送り: its text without a label, code fences or a heading, lines kept."""
    text = re.sub(r"```\w*", "", raw.replace("\r\n", "\n"))
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("###")):
        lines.pop(0)
    if lines:
        lines[0] = _LABEL.sub("", lines[0])
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


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


def note_draft(raw: str, note: str, glossary=None, root: str = "") -> Draft:
    """The model's tidied 備考, written with the glossary's words, and what to check in it."""
    text = clean_note(raw)
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
              wrote: Callable[[str], None], one_line: bool = False) -> str:
        """The reply to `messages` (at most `max_tokens`), `wrote` called with the text so far as it grows; `one_line`: its first line."""
        ...

