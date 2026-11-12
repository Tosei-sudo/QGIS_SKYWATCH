"""
How the 撮像予定 dock tells what became of each request in the shots
(tsudo, 2026-11-11: 「マージした結果、どの範囲になったかも見たい。他方、要求に
どう呼応したかも知りたい」): the history keeps one row per request with its
own area, the shot layer (TBL_REQUEST_SHOT) the outline of each row of the
files; a request names its shot (SHOT_ID), one put into another's names
that request (MERGED_INTO).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from .model import JST
from .store import HistoryRow, ShotRow


def _time(ms: Optional[float]) -> str:
    if ms is None or ms != ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=JST).strftime("%H:%M")


def companions(row: HistoryRow, rows: Sequence[HistoryRow]) -> list[HistoryRow]:
    """The rows taken in the same shot as `row` (itself included, the one whose row it is in the file first); [] when alone."""
    if not row.request_id or row.result != "採用":
        return []
    if row.shot_id:
        same = [r for r in rows if r.shot_id == row.shot_id and r.result == "採用"]
    else:
        lead = row.merged_into or row.request_id
        # Not the request's other times of the day (1 日の回数): each is its own shot.
        same = [r for r in rows if r.result == "採用" and r.system == row.system
                and (r.request_id == lead or r.merged_into == lead) and (r is row or r.request_id != row.request_id)]
    same.sort(key=lambda r: bool(r.merged_into))
    return same if len(same) > 1 else []


def shot_text(row: HistoryRow, rows: Sequence[HistoryRow], shots: dict) -> str:
    """
    The list's 「撮像」: 「22:00 単独」, 「22:00 まとめ 3 件（B・C と）」 for the
    row carrying others, 「22:00 同乗（「A」の撮像）」 for one put into another's
    shot; "" for a request fitted without the orbits or not handed out.
    """
    shot: Optional[ShotRow] = shots.get(row.shot_id) if row.shot_id else None
    when = _time(shot.pass_time) if shot else ""
    together = companions(row, rows)
    if row.merged_into:
        lead = next((r for r in together if r.request_id == row.merged_into), None)
        text = f"同乗（「{(lead.name if lead else '') or row.merged_into}」の撮像）"
    elif together:
        others = [r.name or r.request_id for r in together if r is not row]
        text = f"まとめ {len(together)} 件（{'・'.join(others)} と）"
    elif shot is not None or row.shot_id:
        text = "単独"
    else:
        return ""
    return f"{when} {text}" if when else text


def shot_tip(shot: Optional[ShotRow]) -> str:
    """A shot as the list's tooltip: when, how, how much, which requests."""
    if shot is None:
        return ""
    parts = []
    if shot.pass_time is not None:
        parts.append(f"{_time(shot.pass_time)}（日本時間）")
    if shot.satellite:
        parts.append(shot.satellite)
    if shot.off_nadir is not None:
        parts.append(f"オフナディア角 {shot.off_nadir:.1f}°")
    if shot.incidence is not None:
        parts.append(f"入射角 {shot.incidence:.1f}°")
    parts += [p for p in (shot.orbit_dir, f"{shot.look_side}観測" if shot.look_side else "") if p]
    if shot.scenes is not None:
        parts.append(f"連続 {shot.scenes} シーン" if shot.shots <= 1 else f"{shot.shots} 回の撮像で {shot.scenes} シーン")
    if shot.coverage is not None:
        parts.append(f"範囲の約 {round(shot.coverage * 100)}%")
    text = "・".join(parts)
    if shot.count > 1:
        text += "\n" + f"1 回の撮像に {shot.count} 件: " + "・".join(shot.names or shot.request_ids)
    return text
