"""
「ミカゲと話す」 (core/chat.py, ui/chat_dock.py; tsudo 2026-11-28: 「文で操作の機能は
全廃しよう。名前は任せるけど、チャットで質問も操作もできるように」): its dock and
menu entry (shown while the settings give it a model and the person uses
the generative AI), and what the chat does on the plugin: the operations
that 文で操作 had (its toolbar is gone), 画像検索 behind the screen, and the
images and records its answers list opened.

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

from typing import Sequence

from ..core import errors
from ..core.catalog.api import KINDS, SensorPick, pick_sensors
from ..core.chat import TASK, TITLE
from ..core.obs.api import load_named_areas, settings_for
from ..core.request.api import jst_day
from ..core.text_command import OPERATIONS, Operation, Reading
from ..core.text_request import area_choices, found_choice, point_choice
from ..core.util import ms_of
from ..qgis_io.place_search import place_search_of
from ..qgis_io.tasks import run_task
from ..ui import command_runs as runs
from ..ui.chat_dock import ChatDock
from ..ui.chat_operation import FoundIn, ImagesFound, images_found
from .base import DockSpec, Feature, tr

#: The side of the square taken around a point or a place found by a geocoder (km).
POINT_KM = 5.0
#: How long the named areas read are kept (s).
AREAS_SECONDS = 600


#: Catalog names that say nothing of which one (a catalog named so is not taken as named by a sentence).
PLAIN_CATALOG_NAMES = frozenset({"画像", "カタログ", "画像カタログ", "衛星画像"})


def named_catalogs(text: str, catalogs: Sequence[Any]) -> list:
    """The catalogs a sentence names by their `label` (「レーダーで…」), not by a plain one such as 「画像」."""
    return [c for c in catalogs if len(c.label or "") >= 2 and c.label not in PLAIN_CATALOG_NAMES and c.label in text]


class Chat(Feature):
    COMMANDS = ("open_chat",)

    def __init__(self, host):
        super().__init__(host)
        self._areas: list = []
        self._areas_at = 0.0
        self._areas_waiting: Optional[list] = None

    def dock_specs(self) -> dict:
        iface, context, settings, window = self.iface, self.context, self.host.open_settings, self.window()
        return {"SatObsDataChat": DockSpec(TITLE, "data_chat",
                                           lambda: ChatDock(iface, context, settings, window, self, self._analysis_dock))}

    def connect(self) -> None:
        # Its entry only while its generative AI is given a model and the person uses it.
        self.context.text_assist.changed.connect(self._offered)
        self._offered()

    def _offered(self) -> None:
        """「ミカゲと話す」 in the menu (and its dock) while the settings give it a model and the person uses the generative AI."""
        title = tr(TITLE)
        hidden = self.host.hidden_entries
        offered = self.context.text_assist.has(TASK)
        changed = (title in hidden) == offered
        if offered:
            hidden.discard(title)
        else:
            hidden.add(title)
        if changed:
            self.host.apply_roles()

    def _analysis_dock(self):
        """「データ分析」 shown (its tables read when they are not), for 「データ分析で開く」."""
        dock = self.ports.analysis()
        if dock is not None:
            dock.show()
            dock.raise_()
            if not dock.tables:
                dock.refresh()
        return dock

    def unload(self) -> None:
        try:
            self.context.text_assist.changed.disconnect(self._offered)
        except (TypeError, RuntimeError):
            pass
        if "SatObsDataChat" in self.docks:
            self.docks["SatObsDataChat"].close_all()

    def open_chat(self, text: str = ""):
        """「ミカゲと話す」 shown, with a sentence sent (`plugin.open_chat("…")`; else the field takes the focus); None when not offered."""
        dock = self.docks.get("SatObsDataChat")
        if dock is None or not dock.chat.offered:
            return None
        dock.show()
        dock.raise_()
        if text:
            dock.send(text)
        else:
            dock.field.setFocus()
        return dock

    # ---- what the chat needs (ui/chat_operation.ChatHost) --------------------------------------------

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

    def sensors(self) -> list[str]:
        """The sensors of the catalog chosen in 画像カタログ (as it lists them; none before they are read)."""
        port = self.ports.catalog()
        return list((port.sensor_values() if port is not None else None) or [])

    def pick_sensors(self, words: list[str]) -> Optional[SensorPick]:
        """
        The sensors of the catalog chosen in 画像カタログ that 衛星・種別's words name (core/catalog/sensors.py);
        None when it has no sensor attribute (they can not be searched by).
        """
        port = self.ports.catalog()
        values = port.sensor_values() if port is not None else None
        if values is None:
            return None
        return pick_sensors(words, values, self.satellites(), self.context.known_names())

    def known_place(self, name: str) -> bool:
        """A named area's name (画像検索 without the model: 「呉で今週のSAR画像ある？」)."""
        return bool(name) and name in self.place_names()

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
        client, token = self.context.esri(), None

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
            if not self.host.run_menu_entry(reading.screen, via="COMMAND"):
                raise RuntimeError(tr("「{s}」を開けない").format(s=reading.screen))
            return tr("{s} を開いた").format(s=reading.screen)
        run = self.host.text_operations().get(key)
        if run is None:
            raise RuntimeError(errors.tagged(tr("{o} はいまは使えない（組織の設定・担当）").format(o=reading.operation.title), "E206"))
        if "sensors" in reading.operation.slots:
            reading.catalog_sensors = self._catalog_sensors(reading.sensors)
        return run(reading, place)

    # ---- images ------------------------------------------------------------------------------------------

    def search_images(self, reading: Reading, place: Any, done: Callable[[Optional[ImagesFound], str], None]) -> None:
        """
        画像検索 behind the screen in every catalog 画像カタログ lists, at once (only those the sentence names, when it
        names some; tsudo 2026-11-28 「複数画像カタログがある場合はどうなる？」), its form and list untouched: each
        catalog's sensors 衛星・種別 names, the place's polygon (none: the map's extent). done(found, "") or done(None, why).
        """
        port = self.ports.catalog()
        if port is None or not self.context.config.image_catalogs or not self.host.allows(tr("画像カタログ")):
            done(None, tr("画像カタログを使えない（設定ファイルに imageCatalogs がない、または担当）"))
            return
        catalogs = port.catalogs()
        catalogs = named_catalogs(reading.text, catalogs) or catalogs
        words = list(reading.sensors)
        satellites, known = self.satellites(), self.context.known_names()
        picked: dict[int, list[str]] = {}

        def sensors_for(catalog, values: Optional[list]):
            # (In the search's task: pick_sensors only reads what it is given.)
            if not words or values is None:
                return []  # No 衛星・種別, or a catalog without a sensor attribute: every sensor (as 画像カタログ's search).
            pick = pick_sensors(words, values, satellites, known)
            if pick.sensors:
                picked[id(catalog)] = list(pick.sensors)
                return list(pick.sensors)
            if any(w in KINDS for w in pick.missing) and not pick.listed:
                return tr("センサーの一覧が読めず、{k}で探せない（衛星・種別を消すか、衛星の名前で）").format(
                    k="・".join(w for w in pick.missing if w in KINDS))
            return tr("「{w}」に当たるセンサーがない（衛星・種別を直すか消す）").format(w="、".join(words))

        first, last = reading.period or (None, None)
        where = place.label if place is not None else tr("地図の表示範囲")

        def over(results: list) -> None:
            parts = [FoundIn(c, port.catalog_label(c), sorted(records, key=lambda r: -(ms_of(r.acquired) or 0)), total, problem)
                     for c, records, total, problem in results]
            if not parts or all(p.problem for p in parts):
                several = len(parts) > 1
                done(None, "／".join(f"「{p.label}」: {p.problem}" if several else p.problem for p in parts)
                     or tr("検索できる画像カタログがない"))
                return
            reading.catalog_sensors = list(dict.fromkeys(x for c, *_ in results for x in picked.get(id(c), [])))
            done(images_found(parts, runs.catalog_said(reading, where), lambda r: ms_of(r.acquired) or 0), "")

        port.search_every(first.isoformat() if first else "", last.isoformat() if last else "", runs.place_corners(place),
                          catalogs, sensors_for, over)

    def catalogs(self) -> list:
        """The catalogs 画像カタログ lists (none without it)."""
        port = self.ports.catalog()
        return list(port.catalogs()) if port is not None else []

    def open_images(self, found: ImagesFound, indexes: Sequence[int]) -> str:
        """Those of the images found opened (as from 画像カタログ's list), each with its catalog."""
        port = self.ports.catalog()
        chosen = [(found.catalogs[i], found.records[i]) for i in indexes if 0 <= i < len(found.records)]
        if port is None or not chosen:
            return ""
        groups: dict[int, tuple[Any, list]] = {}
        for catalog, record in chosen:
            groups.setdefault(id(catalog), (catalog, []))[1].append(record)
        for catalog, records in groups.values():
            port.open_catalog_records(catalog, records)
        return tr("画像を開いた: {s}").format(s="、".join(r.id or "?" for _c, r in chosen))

    def show_images(self, found: ImagesFound, part: FoundIn) -> str:
        """A catalog's images found listed in 画像カタログ (that catalog chosen), the dock in front."""
        port = self.ports.catalog()
        if port is None:
            return ""
        said = tr("「{c}」・{s}").format(c=part.label, s=found.said) if len(found.parts) > 1 else found.said
        port.show_event_records(part.catalog, part.records, said)
        return tr("画像カタログに一覧: {s}").format(s=said)

    def open_image_ids(self, ids: Sequence[str]) -> str:
        port = self.ports.catalog()
        if port is None or not ids:
            return ""
        for i in ids:
            port.open_image_id(i)
        return tr("画像 ID で開く: {s}").format(s="、".join(ids))

    def list_image_ids(self, ids: Sequence[str]) -> str:
        port = self.ports.catalog()
        if port is None or not ids:
            return ""
        port.show_image_ids(list(ids), tr(TITLE))
        port.show()
        port.raise_()
        return tr("画像カタログに一覧: {n} 枚の画像 ID").format(n=len(ids))

    def open_records(self, ids: Sequence[str]) -> str:
        """The first record in 観測記録's form (read first when the store is shared)."""
        port = self.observations_if_allowed()()
        if port is None or not ids:
            return tr("観測記録を使えない（担当・設定）") if ids else ""
        first = ids[0]

        def show() -> None:
            book = port.book
            o = book.record(first) if book is not None else None
            if o is None:
                self.iface.statusBarIface().showMessage(tr("観測記録に記録ID「{i}」がない").format(i=first), 8000)
                return
            port.show()
            port.raise_()
            port.open_record(o)

        if port.with_records([first], show):
            show()
        more = tr("（フォームは 1 件ずつ。ほかは「2件目を開いて」のように）") if len(ids) > 1 else ""
        return tr("記録を開いた: {i}").format(i=first) + more

    def _catalog_sensors(self, words: list[str]) -> list[str]:
        """The catalog's sensors 衛星・種別 names (none: every sensor); an error when it names none of them."""
        pick = self.pick_sensors(words) if words else None
        if pick is None:
            return []
        if not pick.sensors:
            if any(w in KINDS for w in pick.missing) and not pick.listed:
                raise RuntimeError(tr("画像カタログのセンサーの一覧が読めず、{k}で探せない（衛星・種別を消すか、衛星の名前で）")
                                   .format(k="・".join(w for w in pick.missing if w in KINDS)))
            raise RuntimeError(tr("「{w}」に当たるセンサーが画像カタログにない（衛星・種別を直すか消す）").format(w="、".join(words)))
        return pick.sensors
