"""
「先読み」 (tsudo, 2026-11-08): the Esri tables the docks read only when
first opened are read in the background right after QGIS has started, so
the dock shows them at once. The settings file's `preload` chooses which
(true: all, the default; false: none; or a list of ITEMS).

Also the startup's own timings (`StartupClock`), told once in QGIS's log
(tab SKYWATCH) and kept in `plugin.api.startup_times`.
"""
from __future__ import annotations

import time
from typing import Optional

#: What can be read ahead, in the order they start: key → what the log calls it.
ITEMS = {
    "satellites": "衛星カタログ",
    "requests": "撮像依頼",
    "events": "イベント",
}
#: Items that were there once and are skipped without a problem (保存した撮像計画, retired 2026-11-09).
RETIRED = {"savedPlans"}


def preload_of(value: object, problems: list[str]) -> tuple[str, ...]:
    """The settings file's `preload`: true (all), false (none) or a list of ITEMS' keys."""
    if value is True:
        return tuple(ITEMS)
    if value is False:
        return ()
    if isinstance(value, list):
        out = []
        for v in value:
            if isinstance(v, str) and v in ITEMS:
                if v not in out:
                    out.append(v)
            elif v in RETIRED:
                continue
            else:
                problems.append(f"preload の「{v}」は不明（使えるのは {'・'.join(ITEMS)}）")
        return tuple(out)
    problems.append("preload は true・false か、先読みするものの一覧（" + "・".join(ITEMS) + "）で指定")
    return tuple(ITEMS)


def seconds(s: float) -> str:
    return f"{s:.2f} 秒" if s < 10 else f"{s:.1f} 秒"


class StartupClock:
    """Steps of the startup with how long each took (wall clock), in the order they ended."""

    def __init__(self, now=time.perf_counter):
        self._now = now
        self.started = now()
        self._last = self.started
        #: (step, seconds) of the startup itself.
        self.steps: list[tuple[str, float]] = []
        #: (item, seconds, ok, message) of the reads ahead.
        self.reads: list[tuple[str, float, bool, str]] = []
        self.ready_at: Optional[float] = None

    def now(self) -> float:
        return self._now()

    def step(self, name: str, took: Optional[float] = None) -> float:
        """A step ended now (it took since the last step ended, or `took`)."""
        t = self._now()
        took = t - self._last if took is None else took
        self._last = t
        self.steps.append((name, took))
        return took

    def lap(self) -> None:
        """What happens from now counts towards the next step (time spent elsewhere, as by QGIS, does not)."""
        self._last = self._now()

    def ready(self) -> None:
        """QGIS has started and the settings file is read: the startup is over."""
        if self.ready_at is None:
            self.ready_at = self._now()

    def read(self, name: str, took: float, ok: bool, message: str = "") -> None:
        self.reads.append((name, took, ok, message))

    def times(self) -> dict:
        """For plugin.api: the steps and the reads ahead, in seconds."""
        return {
            "steps": {n: round(s, 3) for n, s in self.steps},
            "total": round(sum(s for _n, s in self.steps), 3),
            "reads": {n: {"seconds": round(s, 3), "ok": ok, **({"message": m} if m else {})} for n, s, ok, m in self.reads},
        }

    def report(self) -> str:
        """One line for QGIS's log."""
        total = sum(s for _n, s in self.steps)
        text = f"起動 {seconds(total)}（" + "、".join(f"{n} {seconds(s)}" for n, s in self.steps) + "）"
        if self.reads:
            text += "。先読み: " + "、".join(f"{n} {seconds(s)}" + ("" if ok else f"（失敗: {m}）") for n, s, ok, m in self.reads)
        return text
