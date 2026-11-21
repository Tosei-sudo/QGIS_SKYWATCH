"""
撮像計画ドックの雲量 (ui/plan_dock, docs/architecture.md): the cloud forecast of the opportunities and the most cloud wanted. A part of ImagingPlanDock
(ui/imaging_plan_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

import math
from typing import Optional


from ...core.plan.api import Opportunity
from ...core.plan.api import (
    local_text,
)
from ...core.plan.api import apply_clouds, fetch_clouds, places_for
from ...qgis_io import settings
from ...qgis_io.network import get_bytes
from ...qgis_io.tasks import run_task
from .common import _now_ms


class CloudsPart:
    """撮像計画ドックの雲量."""

    def _cloud_toggled(self, on: bool) -> None:
        settings.set_plan_cloud(on)
        if not self.plan:
            return
        if on:
            self.load_clouds()
        else:
            for op in self.plan["result"].opportunities:
                op.cloud = None
            self.cover = None
            self._pairs = None
            self.show_view()

    def _max_cloud_changed(self) -> None:
        self.cover = None
        self._pairs = None
        self.show_view()

    def max_cloud_value(self) -> Optional[float]:
        """The largest cloud cover asked for (percent), or None for any."""
        v = self.max_cloud.value()
        return None if v < 0 else float(v)

    def candidates(self) -> list[Opportunity]:
        """The last plan's opportunities that pass the cloud cover asked for (those without a forecast stay)."""
        ops = self.plan["result"].opportunities if self.plan else []
        top = self.max_cloud_value()
        return list(ops) if top is None else [op for op in ops if op.cloud is None or op.cloud <= top]

    def load_clouds(self) -> None:
        """Asks the forecast service for the cloud cover of the last plan's optical shots (in a task), and lists them with it."""
        plan = self.plan
        config = self.context.config.cloud_forecast
        if not plan or not config or self.cloud_task is not None:
            return
        places = places_for(plan["result"].opportunities, plan["sats"], plan["targets"], _now_ms())
        before = self.status.text()
        if not places:
            self.status.setText(before + self.tr("（雲量予報は 16 日先までの光学衛星の機会のみ）"))
            return
        if config.local:
            self.status.setText(self.tr("{label} の雲量予報ファイルを読み込み中…").format(label=config.label))
        else:
            self.status.setText(self.tr("{label} に雲量予報を問い合わせ中…").format(label=config.label))
        authcfg = None if config.local else settings.auth_for(config.url)

        def work(task):
            return fetch_clouds(config, places, lambda url: get_bytes(url, authcfg), task.isCanceled)

        def done(forecasts):
            self.cloud_task = None
            if self.plan is not plan:
                return
            n = apply_clouds(plan["result"].opportunities, plan["sats"], plan["targets"], forecasts)
            self.cover = None
            self._pairs = None
            self.show_view()
            if self.view.currentData() != "revisit":
                self.status.setText(before + self.tr("（雲量予報 {n} 回分: {label}{issued}）").format(
                    n=n, label=config.label, issued=self._issued_text(forecasts)))

        def failed(message):
            self.cloud_task = None
            self.status.setText(before + self.tr("（雲量予報の取得に失敗: {m}）").format(m=message))

        self.cloud_task = run_task(self.tr("雲量予報の取得"), work, done, failed)

    def _issued_text(self, forecasts) -> str:
        """When a forecast was made, and how many days ago when that is a day or more (empty when not told)."""
        issued = getattr(forecasts, "issued", math.nan)
        if not math.isfinite(issued):
            return ""
        text = self.tr("、{t} の予報").format(t=local_text(issued))
        days = math.floor((_now_ms() - issued) / 86400000)
        return text + (self.tr("（{d} 日前）").format(d=days) if days >= 1 else "")
