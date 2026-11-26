"""
撮像計画ドックの衛星 (ui/plan_dock, docs/architecture.md): the satellites to plan with: the catalog chosen, its outages and the TLEs pasted. A part of ImagingPlanDock
(ui/imaging_plan_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations



from ...core.plan.api import attach
from ...core.request.api import system_names
from ...core.plan.api import BUILT_IN_DEFAULTS, SatelliteSpec, satellites_from_text


class SatellitesPart:
    """撮像計画ドックの衛星."""

    def _catalog_chosen(self, index: int) -> None:
        if index < 0:
            return
        self.catalog_index = index
        self._loaded = False
        self.load_catalog()

    def _outages_changed(self) -> None:
        """The outages known changed (entered, read again): the satellites carry them and the list shows them."""
        if not self.catalog_sats:
            return
        attached = attach(self.catalog_sats, self.context.known_outages(), system_names(self.context.config.request_exports))
        if any(a is not b for a, b in zip(attached, self.catalog_sats)):
            self.catalog_sats = attached
            self.list_sats()

    def pasted(self) -> list[SatelliteSpec]:
        return satellites_from_text(
            self.tle.toPlainText(),
            maxOffNadir=self.paste_max.value() or BUILT_IN_DEFAULTS["maxOffNadir"],
            minOffNadir=0,
            swath=self.paste_swath.value() or BUILT_IN_DEFAULTS["swath"],
            minInterval=self.paste_gap.value(),
            maxPerDay=self.paste_per_day.value(),
            leadTime=self.paste_lead.value(),
            kind=self.paste_kind.currentData(),
        )

    def list_sats(self) -> None:
        self.tree.set([*self.catalog_sats, *self.pasted()])
