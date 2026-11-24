"""
文で操作 (core/text_command.py, ui/text_command.py; tsudo 2026-11-21): the
toolbar's field and what each operation does with the values on its card.

Each feature does the operations on its own docks (`text_operations`,
ui/command_runs.py): the screen opened with the values in it, then only
what its own button would that reads (計算, 検索, 表示); what writes, imports,
saves or sends (書き出し, 取り込み, 照合, 登録, 送信) is left to the screen's own
button and its question. An operation is offered when a feature offers it
(its dock there, the settings having what it needs) and the roles in use
(担当) have its screen; 「画面を開く」 is this feature's (the menu's entries).
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable, Optional

from ..core import errors
from ..core.obs.api import load_named_areas, settings_for
from ..core.request.api import jst_day
from ..core.text_command import OPERATIONS, Operation, Reading
from ..core.text_request import area_choices, found_choice, point_choice
from ..qgis_io.place_search import place_search_of
from ..qgis_io.tasks import run_task
from ..ui.text_command import CommandBox
from .base import Feature, close, tr

#: The side of the square taken around a point or a place found by a geocoder (km).
POINT_KM = 5.0
#: How long the named areas read are kept (s).
AREAS_SECONDS = 600


class Command(Feature):
    COMMANDS = ("open_command",)

    def __init__(self, host):
        super().__init__(host)
        #: 「文で操作」 (ui/text_command.py), made once the other features' entries are.
        host.command_box = None
        self._areas: list = []
        self._areas_at = 0.0
        self._areas_waiting: Optional[list] = None

    def add_actions(self) -> None:
        # After every dock and entry: the operations open them.
        self.host.command_box = CommandBox(self.iface, self.context, self, self.window())

    def roles_changed(self) -> None:
        if self.host.command_box is not None:
            self.host.command_box.apply()

    def unload(self) -> None:
        close(self.host.command_box)
        self.host.command_box = None

    def open_command(self, text: str = ""):
        """「文で操作」 with a sentence (`plugin.open_command("…")`; the field takes the focus when there is none)."""
        box = self.host.command_box
        if box is None or not box.offered:
            return None
        if text:
            box.field.setText(text)
            box.submit(text)
        else:
            box.field.setFocus()
        return box

    # ---- what the card needs (ui/text_command.CommandHost) -------------------------------------------

    def today(self) -> date:
        return jst_day(time.time() * 1000)

    def operations(self) -> list[Operation]:
        """The operations the features offer now (their docks, the settings) that the roles in use have, in OPERATIONS' order."""
        host = self.host
        runs = host.text_operations()
        return [op for op in OPERATIONS if (op.key in runs or op.key == "openScreen")
                and (not op.feature or host.allows(tr(op.feature)))]

    def systems(self) -> list:
        return list(self.context.config.request_exports)

    def place_names(self) -> list[str]:
        self._read_areas()
        return [a.name for a in self._areas if a.name]

    def satellites(self) -> list:
        port = self.ports.satellites()
        if port is None:
            return []
        known = port.known()
        if not known:
            port.read(lambda _sats: None, lambda _m: None)
        return list(known)

    def screens(self) -> list[str]:
        return self.host.menu_texts()

    def ask_data(self, text: str) -> bool:
        """A question on the data typed in the field: asked in 「データに質問」 (False when it is not offered)."""
        return self.host.open_data_chat(text) is not None

    def _named_source(self):
        c = self.context.config
        return settings_for(c.observation_exports, c.alert_reports).named_areas

    def _read_areas(self, then: Optional[Callable[[], None]] = None) -> None:
        """The named areas (read in a task, kept AREAS_SECONDS), then `then` (at once when read)."""
        source = self._named_source()
        if source is None or time.monotonic() - self._areas_at < AREAS_SECONDS:
            if then is not None:
                then()
            return
        if self._areas_waiting is not None:
            if then is not None:
                self._areas_waiting.append(then)
            return
        self._areas_waiting = [then] if then is not None else []
        client, token = self.context.esri(), self.context.token_for(source.url, source.token)

        def over(areas) -> None:
            if areas is not None:
                self._areas, self._areas_at = list(areas), time.monotonic()
            waiting, self._areas_waiting = self._areas_waiting or [], None
            for w in waiting:
                w()

        run_task(tr("地名の取得"), lambda _t: load_named_areas(client, source, token, max_age=AREAS_SECONDS),
                 over, lambda _m: over(None), hidden=True)

    def find_place(self, text: str, point, then: Callable[[Any, str], None]) -> None:
        if point is not None:
            then(point_choice(point[0], point[1], POINT_KM), "")
            return

        def with_areas() -> None:
            choices = area_choices(text, self._areas)
            if choices:
                then(choices[0], "")
                return
            search = place_search_of(self.context)
            if not search.enabled:
                then(None, tr("地名テーブルに「{t}」なし（緯度経度でも）").format(t=text))
                return

            def found(result) -> None:
                places, errors = result
                if places:
                    then(found_choice(text, places[0], POINT_KM), "")
                else:
                    then(None, tr("見つからず（{e}）").format(e="、".join(errors)) if errors else tr("見つからず"))

            run_task(tr("地名・住所の検索"), lambda task: search.search(text, task.isCanceled), found,
                     lambda m: then(None, m), hidden=True)

        self._read_areas(with_areas)


    # ---- doing it ----------------------------------------------------------------------------------------

    def run(self, reading: Reading, place: Any) -> str:
        """The operation done by the feature that has it, with the card's values; what was done."""
        key = reading.operation.key
        if key == "openScreen":
            if not self.host.run_menu_entry(reading.screen):
                raise RuntimeError(tr("「{s}」を開けない").format(s=reading.screen))
            return tr("{s} を開いた").format(s=reading.screen)
        run = self.host.text_operations().get(key)
        if run is None:
            raise RuntimeError(errors.tagged(tr("{o} はいまは使えない（設定ファイル・担当）").format(o=reading.operation.title), "E206"))
        return run(reading, place)
