"""
判読の練習's sample exercises on the built-in sample (Tokyo Bay, Yokosuka): made
from the sample's images as they were drawn (files/catalog/images.json: each
image's ships, where, how long and wide, which way), so the answers are where
the ships are. An introduction until the organisation has its own exercises
(its tables, `training.exercises`).

The sample's four ships (sample.SHIPS, by their number `n`) are of four
classes, told apart by their size and look; an optical image draws every
other ship under way (a wake behind it: 航行), the others at anchor (停泊).
"""
from __future__ import annotations

from typing import Optional

from .exercises import (
    Answer, CAME, CHANGE, DEFAULT_SCORING, EXAM, Exercise, FIND, GONE, IDENTIFY, MEASURE, PRACTICE, Scoring, STAYED,
)

COURSE = "船の判読（入門・横須賀の見本）"
SOURCE = "見本"
TARGET = "船舶"
#: Each ship's class and how to tell it (by its number in sample.SHIPS).
CLASSES = {
    0: ("船舶 > 貨物船 > コンテナ船", "長さ約 180 m・幅約 28 m の大きな船。甲板に色とりどりのコンテナが並ぶのでコンテナ船です。"),
    1: ("船舶 > 支援船 > 〇〇級", "長さ約 120 m・幅約 20 m。甲板にコンテナがなく、白い上部構造が船尾にあります。"
        "この見本では、長さ 110〜130 m の支援船を〇〇級とします。"),
    2: ("船舶 > 貨物船", "長さ約 90 m・幅約 16 m の中くらいの船。コンテナはなく、上部構造が船尾にある一般の貨物船です。"),
    3: ("船舶 > 支援船", "長さ約 60 m・幅約 12 m の小さな船。級までは見分けられないので、支援船までにします。"),
}
UNDER_WAY, AT_ANCHOR = "航行", "停泊"
WAKE = "船尾の後ろに白い航跡があるので航行中です。"
NO_WAKE = "航跡がないので停泊（止まっている）と判断します。"
SAR_NOTE = "SAR 画像では色や航跡が見えないため、明るく写る長さと形で見分けます。"


def _box_ring(box) -> list:
    w, s, e, n = box
    return [[w, s], [w, n], [e, n], [e, s], [w, s]]


def _inset(box, k: float = 0.06) -> list:
    """The image's footprint without its edges (a thing cut by the edge is not asked for)."""
    w, s, e, n = box
    dx, dy = (e - w) * k, (n - s) * k
    return [_box_ring((w + dx, s + dy, e - dx, n - dy))]


def _activity(plan: dict, index: int) -> str:
    """What the drawn image shows the ship doing (the optical images: every other one has a wake; SAR: not seen)."""
    if plan.get("sar"):
        return ""
    return UNDER_WAY if index % 2 == 1 else AT_ANCHOR


def _answer(plan: dict, index: int, ship: dict, points: float = 10.0, measure: bool = False, change: str = "",
            where: Optional[dict] = None) -> Answer:
    kind, why = CLASSES.get(ship.get("n", -1), ("船舶", ""))
    activity = "" if change == GONE else _activity(plan, index)
    explanation = why
    if activity:
        explanation += (WAKE if activity == UNDER_WAY else NO_WAKE)
    elif plan.get("sar"):
        explanation += SAR_NOTE
    at = where or ship
    return Answer(lon=at["lon"], lat=at["lat"], kind=kind, activity=activity,
                  length=float(ship["length"]) if measure else None, width=float(ship["width"]) if measure else None,
                  heading=round(float(ship["heading"]) % 360, 1) if measure else None, change=change, points=points,
                  explanation=explanation)


def _plan(plans: list, k: int) -> Optional[dict]:
    return plans[k] if 0 <= k < len(plans) and isinstance(plans[k], dict) and plans[k].get("ships") is not None else None


def sample_exercises(plans: list, scoring: Scoring = DEFAULT_SCORING) -> list:
    """The sample's exercises over its images (`plans`: images.json), in their order ([] when the images are not those)."""
    if len(plans) < 6:
        return []
    out = []
    p5, p3, p2, p4, p0 = (_plan(plans, k) for k in (5, 3, 2, 4, 0))
    if p5:
        out.append(Exercise(
            "sample-find", "船を見つける", FIND, p5["id"], _inset(p5["box"]),
            "範囲（橙の点線）の中の、長さ 50 m 以上の船をすべてクリック記録し、種別と活動を入れてください。"
            "白い小さな点（小舟）は数えません。", target_kinds=[TARGET], difficulty="易", mode=PRACTICE,
            answers=[_answer(p5, i, s) for i, s in enumerate(p5["ships"])]))
    if p3:
        out.append(Exercise(
            "sample-identify", "船の種別と活動を見分ける", IDENTIFY, p3["id"], _inset(p3["box"]),
            "赤い番号の印の船を、1 隻ずつクリック記録してください。種別は級まで、活動は航跡を見て選びます。",
            target_kinds=[TARGET], difficulty="易", mode=PRACTICE,
            answers=[_answer(p3, i, s) for i, s in enumerate(p3["ships"])]))
    if p2:
        out.append(Exercise(
            "sample-measure", "船の長さ・幅・向きを測る", MEASURE, p2["id"], _inset(p2["box"]),
            "赤い番号の印の船を、入力点数「3 点」で測ってください（船尾 → 船首 → 片側の舷側の順にクリック）。"
            "種別は分かる所までで構いません。", target_kinds=[TARGET], difficulty="中", mode=PRACTICE,
            answers=[_answer(p2, i, s, measure=True) for i, s in enumerate(p2["ships"])]))
    if p2 and p3:
        before = {s["n"]: s for s in p2["ships"]}
        after = {s["n"]: s for s in p3["ships"]}
        answers = []
        for i, s in enumerate(p3["ships"]):
            answers.append(_answer(p3, i, s, change=STAYED if s["n"] in before else CAME))
        for s in p2["ships"]:
            if s["n"] not in after:
                answers.append(_answer(p2, 0, s, change=GONE))
        out.append(Exercise(
            "sample-change", "前の画像からの出入りを見つける", CHANGE, p3["id"], _inset(p3["box"]),
            "前の画像で記録した船が、白抜きの印で出ています。まだいる船は印をクリックして「継続」、いなくなった船は「消失」にし、"
            "新しく来た船はクリック記録してください。", before_image_id=p2["id"], target_kinds=[TARGET], difficulty="中", mode=PRACTICE,
            answers=answers))
    if p4:
        out.append(Exercise(
            "sample-sar", "SAR 画像で船を見つける", FIND, p4["id"], _inset(p4["box"]),
            "SAR 画像の範囲の中の船をすべてクリック記録し、種別を入れてください（活動は付けなくて構いません）。"
            "陸の明るい点（建物）は船ではありません。", target_kinds=[TARGET], difficulty="難", mode=PRACTICE,
            answers=[_answer(p4, i, s) for i, s in enumerate(p4["ships"])]))
    if p0:
        out.append(Exercise(
            "sample-exam", "確認試験（船の判読・入門）", FIND, p0["id"], _inset(p0["box"]),
            "範囲の中の、長さ 50 m 以上の船をすべてクリック記録し、種別と活動を入れて、時間内に提出してください。"
            "ヒントはありません。提出は 1 回だけです。", target_kinds=[TARGET], difficulty="中", time_limit=10, mode=EXAM,
            answers=[_answer(p0, i, s) for i, s in enumerate(p0["ships"])]))
    for order, e in enumerate(out, start=1):
        e.course, e.order, e.source, e.roles = COURSE, order, SOURCE, ["OBSERVER"]
        if e.scoring is DEFAULT_SCORING:
            e.scoring = scoring
        e.numbered()
    return out
