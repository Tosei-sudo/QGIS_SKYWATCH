"""
The imaging plan's shots outside the dock, as the viewer
(imaging-plan-remind.ts): an iCalendar file (.ics) for Outlook and other
calendars, and reminders a chosen number of minutes before each shot.

The viewer keeps reminders in the browser and turns them into notices of its
notification centre; the plugin keeps them in the QGIS settings
(qgis_io/settings.py) and shows them in the message bar and the log. This
module is the plain part: which shots, the file, and which reminders are due.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from .access import Opportunity, PlanTarget
from .satellites import SatelliteSpec, satellite_label

#: The most reminders kept.
MAX_REMINDERS = 500


@dataclass
class Shot:
    """One shot: a satellite at a moment, with the targets it takes (a shared scene once)."""
    op: Opportunity
    sat: SatelliteSpec
    targets: list


@dataclass
class PlanReminder:
    """One shot to remind of."""
    #: Satellite id and time, so the same shot is not reminded of twice.
    id: str
    time: float
    notifyAt: float
    satellite: str
    target: str
    offNadir: float
    #: The scenes, longitude / latitude rings (to show them from the notice).
    rings: list

    def to_json(self) -> dict:
        return asdict(self)


def reminder_of(value: Any) -> Optional[PlanReminder]:
    """A reminder read back from the settings; None when it is not one."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        return None
    try:
        notify = float(value["notifyAt"])
        if not math.isfinite(notify):
            return None
        return PlanReminder(value["id"], float(value["time"]), notify, str(value.get("satellite", "")), str(value.get("target", "")),
                            float(value.get("offNadir", 0)), list(value.get("rings") or []))
    except (KeyError, TypeError, ValueError):
        return None


def shots_of(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget]) -> list[Shot]:
    """
    The shots among `ops` (a combination's scenes taken once even when they
    take several targets): one per satellite and moment, with the targets
    each takes, in time order.
    """
    by_key: dict[str, Shot] = {}
    for op in ops:
        sat = sats[op.satellite]
        key = f"shot:{op.shot}" if op.shot is not None else f"{sat.id}:{op.time}"
        label = targets[op.target].label if 0 <= op.target < len(targets) else ""
        found = by_key.get(key)
        if found:
            if label not in found.targets:
                found.targets.append(label)
            found.op = op if op.time < found.op.time else found.op
        else:
            by_key[key] = Shot(op, sat, [label])
    return sorted(by_key.values(), key=lambda s: s.op.time)


def _ics_time(ms: float) -> str:
    """`20261022T201500Z`."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(ms: float) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _ics_text(text: str) -> str:
    """Text escaped for an iCalendar value."""
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """A content line folded at 75 octets (RFC 5545 3.1), without splitting a character."""
    parts: list[str] = []
    current, size = "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (74 if parts else 75):
            parts.append(current)
            current, size = "", 0
        current += ch
        size += n
    parts.append(current)
    return "\r\n ".join(parts)


def _fixed(v: float, digits: int) -> str:
    """JavaScript's toFixed: the nearer, the larger on a tie, keeping the sign of a small negative (`-0.0`)."""
    p = 10 ** digits
    n = math.floor(v * p + 0.5)
    text = f"{abs(n) / p:.{digits}f}"
    return f"-{text}" if v < 0 else text


def plan_ics(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget],
             alarm: Optional[float] = None, now: Optional[float] = None) -> str:
    """The shots as an iCalendar file: an event a minute long at each, with an alarm `alarm` minutes before (none when None)."""
    now = datetime.now(timezone.utc).timestamp() * 1000 if now is None else now
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//browser-image-enhancement//image-viewer imaging plan//JA", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for shot in shots_of(ops, sats, targets):
        op, sat, names = shot.op, shot.sat, shot.targets
        end = max(op.time + 60000, op.scene[1] if op.scene else 0)
        details = [
            f"衛星: {sat.name}（{sat.id}）",
            *([f"撮像モード: {sat.mode}"] if sat.mode else []),
            f"対象: {'、'.join(names)}",
            f"オフナディア角: {_fixed(op.offNadir, 1)}°・入射角 {_fixed(op.incidence, 1)}°",
            f"方向: {'右' if op.side == 'right' else '左'}・{'北行' if op.ascending else '南行'}",
            f"太陽高度: {_fixed(op.sunElevation, 0)}°",
            *([f"発注期限: {_iso(op.time - sat.leadTime * 3600000)}"] if sat.leadTime > 0 else []),
        ]
        center = targets[op.target].center
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_ics_text(f'{sat.id}-{op.time}')}@image-viewer",
            f"DTSTAMP:{_ics_time(now)}",
            f"DTSTART:{_ics_time(op.time)}",
            f"DTEND:{_ics_time(end)}",
            f"SUMMARY:{_ics_text(f'撮像 {satellite_label(sat)} → ' + '、'.join(names))}",
            f"DESCRIPTION:{_ics_text(chr(10).join(details))}",
            f"GEO:{center[1]:.6f};{center[0]:.6f}",
            "TRANSP:TRANSPARENT",
        ]
        if alarm is not None:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(f'撮像 {satellite_label(sat)}')}",
                      f"TRIGGER:-PT{max(0, math.floor(alarm + 0.5))}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def new_reminders(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget], minutes: float,
                  rings_of: Callable[[Opportunity], list], now: float) -> list[PlanReminder]:
    """Reminders `minutes` before the shots of `ops` still ahead; `rings_of` gives each shot's scenes."""
    shots = [s for s in shots_of(ops, sats, targets) if s.op.time > now][:MAX_REMINDERS]
    return [PlanReminder(
        id=f"{s.sat.id}:{s.op.time}", time=s.op.time, notifyAt=s.op.time - max(0.0, minutes) * 60000,
        satellite=satellite_label(s.sat), target="、".join(s.targets), offNadir=math.floor(s.op.offNadir * 10 + 0.5) / 10,
        rings=rings_of(s.op)) for s in shots]


def merge_reminders(kept: Sequence[PlanReminder], fresh: Sequence[PlanReminder]) -> list[PlanReminder]:
    """`fresh` replacing those of the same shots in `kept`, soonest first, at most MAX_REMINDERS."""
    ids = {r.id for r in fresh}
    return sorted([*(r for r in kept if r.id not in ids), *fresh], key=lambda r: r.notifyAt)[:MAX_REMINDERS]


def due_reminders(kept: Sequence[PlanReminder], now: float) -> tuple[list[PlanReminder], list[PlanReminder]]:
    """
    The reminders to tell of now, and those to keep: due ones are taken out;
    of them, shots more than 10 minutes past are dropped without a notice.
    """
    due = [r for r in kept if r.notifyAt <= now]
    rest = [r for r in kept if r.notifyAt > now]
    return [r for r in due if r.time > now - 10 * 60000], rest


def _time_text(ms: float) -> str:
    d = datetime.fromtimestamp(ms / 1000)
    return f"{d.month}/{d.day} {d.hour:02d}:{d.minute:02d}"


def reminder_text(r: PlanReminder, now: float) -> str:
    """The notice of a reminder: 「10/23 10:00 WorldView-3 が撮像（あと 30 分）・東京・オフナディア角 10.2°」."""
    left = max(0, math.floor((r.time - now) / 60000 + 0.5))
    return f"{_time_text(r.time)} {r.satellite} が撮像{f'（あと {left} 分）' if left > 0 else ''}・{r.target}・オフナディア角 {r.offNadir:g}°"
