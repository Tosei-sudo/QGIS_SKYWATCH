"""
What the plugin's parts share: the settings file read (and what was wrong
in it), the Esri client and the local GeoPackage.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal

from ..core import errors, sign_in
from ..core.config import PluginConfig, load_jsonc, parse_config, parse_config_text, read_config_file
from ..core.esri import EsriClient, LayerInfos
from ..core.gp import service_root as gp_service_root
from ..core.gpkg import GeoPackage, open_local
from ..core.obs.api import place_cache
from ..core.obs.api import SatelliteChoices, load_satellite_choices
from ..core.offline import OfflineWork
from ..core.plan.api import DEFAULT_CLOUD_FORECAST
from ..core.plan.api import NO_NAMES, SatelliteNames, load_satellite_names
from ..core.plan.api import Outage, load_outages
from ..core.edit_stamp import set_tracking
from ..core.time_zone import set_rules
from . import settings, training_io
from .network import get_bytes, qgis_transport
from .tasks import run_task


def _web(url: object) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


@dataclass
class Service:
    """A service of the settings file: what it is for, its name and URL."""
    role: str
    label: str
    url: str


class PluginContext(QObject):
    """The settings file and the connections, shared by the docks."""

    #: The settings file was read again.
    configChanged = pyqtSignal()
    #: The satellite outages read differ from those read before (set_outages).
    outagesChanged = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = PluginConfig()
        self.problems: list[str] = []
        self.source = ""
        #: The settings file read is the one shipped in the plugin's folder.
        self.bundled = False
        #: Only the shipped settings file is read (a file chosen in the settings dialog is ignored).
        self.locked = False
        self.loading = False
        #: The text of a settings file read from a URL (None: a local file, or none read; the audit log's hash of it).
        self.config_text: Optional[str] = None
        #: Counts reloads: only the latest one's answer is taken.
        self._generation = 0
        self._gpkg: Optional[GeoPackage] = None
        #: The satellite name tables (`satelliteAliases`) once read; None until then (read again after a reload).
        self._names: Optional[SatelliteNames] = None
        self._names_lock = threading.Lock()
        #: 衛星名 known (core/obs/satellites.py: the image catalogs' sensors, the satellite catalogs' names), once read.
        self._satellite_choices: Optional[SatelliteChoices] = None
        #: サインイン (`signIn`): set for the requests before any other part hears of new settings (qgis_io/sign_in_io.py).
        from .sign_in_io import SignInKeeper

        self.sign_in = SignInKeeper(self)
        self.configChanged.connect(self._forget_names)
        #: 衛星の障害情報 (`satelliteOutages`) as last read (read again for each calculation; outagesChanged when they change).
        self._outages: tuple = ()
        self.configChanged.connect(lambda: self.set_outages(()))
        # First of all who hear it: every request after a reload reads dates by the new file's time zones.
        self.configChanged.connect(lambda: set_rules(self.config.time_zones))
        self.configChanged.connect(lambda: set_tracking(self.config.edit_tracking))
        #: The layers' information, kept across the clients (esri()) for ten minutes, and forgotten with the settings.
        self.layer_infos = LayerInfos()
        self.configChanged.connect(self.layer_infos.clear)
        #: 一時的なオフライン作業: the cache of reads and the changes waiting for a server down (core/offline.py).
        self.offline = OfflineWork(settings.offline_path())
        self.configChanged.connect(self._offline_settings)
        #: 地名・国の控え: the squares looked up and the polygons read, kept across sessions (core/obs/place_cache.py).
        place_cache.PLACES.use_file(settings.place_cache_path())
        #: 備考の単語集 (`glossary`): read in a task when the settings file is read (qgis_io/glossary_io.py).
        from .glossary_io import GlossaryKeeper

        self.glossary = GlossaryKeeper(self)
        #: 文章の手伝い (`textAssist`): the small generative model, loaded when first asked (qgis_io/text_assist_io.py).
        from .text_assist_io import TextAssistant

        self.text_assist = TextAssistant(self)

    def reload(self) -> None:
        """
        Reads the settings file again (a URL in a task): the one chosen in the
        settings dialog, else the one shipped in the plugin's folder; the
        defaults when there is neither. Only the shipped one when the zip
        locks the plugin to it (config.lock).
        """
        source, bundled = settings.effective_config_source()
        self.source = source
        self.bundled = bundled
        self.locked = bundled and settings.config_locked()
        self.config_text = None
        if not source:
            self.config, self.problems = PluginConfig(), []
            self.configChanged.emit()
            return
        self._generation += 1
        generation = self._generation
        if re.match(r"^https?://", source, re.IGNORECASE):
            self.loading = True

            def work(task):
                return get_bytes(source, settings.auth_for(source)).decode("utf-8-sig", errors="replace")

            def done(text):
                if generation != self._generation:
                    return  # An older answer: a later reload is the one that counts (#85).
                self.loading = False
                self.config_text = text
                config, self.problems = parse_config_text(text)
                self.config = training_io.shown(config, source)
                try:
                    settings.set_cached_config(source, load_jsonc(text), time.strftime("%Y-%m-%d %H:%M"))
                except ValueError:
                    pass
                self.configChanged.emit()

            def failed(message):
                if generation != self._generation:
                    return
                self.loading = False
                cached = settings.cached_config(source)
                if cached is not None:
                    # The last file read, rather than nothing (the stores would all be gone, #85); its tokens are not kept.
                    value, when = cached
                    config, problems = parse_config(value)
                    self.config = training_io.shown(config, source)
                    # (The reason's エラーコード at the end, core/errors.py.)
                    self.problems = [errors.tagged(f"設定ファイルの取得に失敗: {errors.untagged(message)}。前回{f'（{when}）' if when else ''}"
                                                   "読めた設定で動作中（トークンは控えていないため、認証設定を選んでいないサービスは使えない場合あり）",
                                                   errors.code_of(message)), *problems]
                else:
                    self.config, self.problems = PluginConfig(), [f"設定ファイルの取得に失敗: {message}"]
                self.configChanged.emit()

            run_task("設定ファイルの読み込み", work, done, failed)
            return
        config, self.problems = read_config_file(source)
        self.config = training_io.shown(config, source)
        self.configChanged.emit()

    def esri(self) -> EsriClient:
        """
        An Esri client whose requests follow QGIS's proxy and the authcfg chosen
        per service (use inside a task). Its reads are kept for offline work and
        answered from there while their server is down (core/offline.py). The
        layers' information it asks is kept for the next clients (LayerInfos).
        """
        # (A token of the settings file's `signIn` refused by a server is asked again: core/sign_in.py.)
        client = EsriClient(self.offline.transport(qgis_transport(settings.auth_for)), renew=sign_in.renew_for, layers=self.layer_infos)
        client.offline = self.offline
        return client

    def _offline_settings(self) -> None:
        o = self.config.offline_work
        self.offline.enabled = o.enabled
        self.offline.cache_bytes = int(o.cache_mb * 1024 * 1024)

    def _forget_names(self) -> None:
        with self._names_lock:
            self._names = None
            self._satellite_choices = None

    def satellite_choices(self, client: Optional[EsriClient] = None, cancelled=None) -> SatelliteChoices:
        """
        The satellite names known (use inside a task: the image catalogs'
        sensors and the satellite catalogs' names are read the first time,
        then kept until the settings file is read again), with their other names.
        """
        with self._names_lock:
            if self._satellite_choices is not None:
                return self._satellite_choices
        config = self.config
        client = client or self.esri()
        names = self.satellite_names(client, cancelled)
        try:
            choices, whole = load_satellite_choices(client, config.image_catalogs, config.satellite_catalogs, names, cancelled)
        except Exception:  # noqa: BLE001 (names only help; the records are kept without them)
            return SatelliteChoices(names=names)
        with self._names_lock:
            # Kept when every table was read (the alias tables too: satellite_names keeps them then).
            if whole and self.config is config and (self._names is not None or not config.satellite_aliases):
                self._satellite_choices = choices
        return choices

    def satellite_choices_read(self) -> bool:
        """Whether the satellite names have been read for this settings file."""
        with self._names_lock:
            return self._satellite_choices is not None

    def known_satellite_choices(self) -> SatelliteChoices:
        """The satellite names already read (for the main thread, which does not wait for the tables)."""
        with self._names_lock:
            if self._satellite_choices is not None:
                return self._satellite_choices
            return SatelliteChoices(names=self._names or NO_NAMES)

    def satellite_names(self, client: Optional[EsriClient] = None, cancelled=None) -> SatelliteNames:
        """
        The satellites' other names of `satelliteAliases` (use inside a task:
        the tables are read the first time, then kept until the settings file
        is read again). None configured, or unreadable: no other names.
        """
        tables = self.config.satellite_aliases
        if not tables:
            return NO_NAMES
        with self._names_lock:
            if self._names is not None:
                return self._names
        try:
            names, problems = load_satellite_names(client or self.esri(), tables, cancelled)
        except Exception:  # noqa: BLE001 (names only make matches better; the work goes on without them)
            return NO_NAMES
        with self._names_lock:
            # Kept only when every table was read (one that failed is asked again next time).
            if not problems and self.config.satellite_aliases is tables:
                self._names = names
        return names

    def read_outages(self, client: Optional[EsriClient] = None, cancelled=None) -> tuple[list[Outage], str]:
        """
        The satellite outages of `satelliteOutages` (use inside a task; the
        table is small and read each time, so that one just entered counts),
        and why they could not be read ("" when they were, or none is set).
        Unreadable: those read last time.
        """
        source = self.config.satellite_outages
        if source is None:
            return [], ""
        try:
            return load_outages(client or self.esri(), source, None, cancelled), ""
        except Exception as error:  # noqa: BLE001 (the calculation goes on with those known, said so)
            return list(self._outages), f"衛星の障害情報を読めず、前に読んだ {len(self._outages)} 件で計算（{error}）"

    def known_outages(self) -> list[Outage]:
        """The outages last read (for the main thread, which does not wait for the table)."""
        return list(self._outages)

    def set_outages(self, outages) -> None:
        """Keeps the outages read (main thread); `outagesChanged` when they differ from those kept."""
        outages = tuple(outages)
        if outages != self._outages:
            self._outages = outages
            self.outagesChanged.emit()

    def known_names(self) -> SatelliteNames:
        """The other names already read (for the main thread, which does not wait for the tables)."""
        with self._names_lock:
            return self._names or NO_NAMES

    def gpkg(self) -> GeoPackage:
        """The local GeoPackage (opened once)."""
        if self._gpkg is None:
            self._gpkg = open_local(settings.local_gpkg_path())
        return self._gpkg

    def switch_files(self) -> None:
        """
        The local files again from the settings (練習モード switched on or off: qgis_io/training_io.py): the
        offline work, the place cache and the local GeoPackage (opened again when next asked for).
        """
        self.offline.use_file(settings.offline_path())
        place_cache.PLACES.use_file(settings.place_cache_path())
        if self._gpkg is not None:
            self._gpkg.close()
            self._gpkg = None

    def release_files(self) -> None:
        """The local files closed (練習データを最初に戻す: the practice's are thrown away); switch_files opens them again."""
        self.offline.close()
        place_cache.PLACES.close()
        if self._gpkg is not None:
            self._gpkg.close()
            self._gpkg = None

    def close(self) -> None:
        self.sign_in.close()
        self.offline.close()
        place_cache.PLACES.close()
        if self._gpkg is not None:
            self._gpkg.close()
            self._gpkg = None

    def services(self) -> list[Service]:
        """Every service of the settings file that may need a sign-in, for choosing their authentication."""
        c = self.config
        out: list[Service] = []
        out += [Service("画像カタログ", x.label, x.url) for x in c.image_catalogs]
        # (練習モードだけのカタログ: 判読練習の指導者 open them in production too)
        out += [Service("画像カタログ", f"{x.label}（練習用）", x.url) for x in c.practice_catalogs]
        out += [Service("衛星カタログ", x.label, x.url) for x in c.satellite_catalogs]
        out += [Service("センサーカタログ", x.label, x.url) for x in c.sensor_catalogs if x.url]
        out += [Service("衛星名の対応表", x.label, x.url) for x in c.satellite_aliases]
        if c.satellite_outages is not None:
            out.append(Service("衛星の障害情報", "衛星の障害情報", c.satellite_outages.url))
        for x in c.observation_stores:
            out.append(Service("観測記録の保存先", x.label, x.url))
            if x.areas:
                out.append(Service("確認範囲", x.label, x.areas.url))
            if x.taxonomy_table:
                out.append(Service("分類体系", x.label, x.taxonomy_table.url))
        if c.glossary is not None:
            out.append(Service("単語集", "備考の単語集", c.glossary.url))
        # Base maps on an ArcGIS server (their token is not put in the layer: an authentication configuration is).
        out += [Service("背景地図", x.label, x.url.rstrip("/")) for x in c.base_maps if x.type == "esri"]
        # Processing services, under the URL their sign-in is kept for (the GPServer, or the OGC API landing page).
        out += [Service("サーバ処理", x.label, gp_service_root(x.url)) for x in c.gp_services]
        # The request ledger and its history, AI results, the weather and the rest (#75).
        for x in c.request_stores:
            out.append(Service("撮像依頼台帳", x.label, x.url))
            if x.history:
                out.append(Service("出力履歴", x.label, x.history))
        out += [Service("AI 結果の送信先", x.label, x.url) for x in c.detection_outputs]
        out += [Service("外部AI結果", x.label, x.url) for x in c.ai_result_sources]
        if c.cloud_forecast is not None and _web(getattr(c.cloud_forecast, "url", "")) and c.cloud_forecast.url != DEFAULT_CLOUD_FORECAST.url:
            out.append(Service("雲量予報", c.cloud_forecast.label, c.cloud_forecast.url))
        out += [Service("移動体の情報源", x.label, x.url) for x in c.moving_feeds if _web(x.url)]
        out += [Service("地名検索", x.label, x.url) for x in c.geocoders if _web(x.url)]
        out += [Service("起動時レイヤー", x.layer or x.url, x.url) for x in c.startup_layers if _web(x.url)]
        for x in c.ai_models:
            for url in (getattr(x, name, "") for name in ("url", "encoder", "decoder")):
                if _web(url):
                    out.append(Service("AI モデル", x.label, url))
        for m in c.text_assist.models if c.text_assist is not None else ():
            if _web(m.path):
                out.append(Service("文章の手伝い", m.name, m.path))
        # One row per URL: the first role that names it.
        seen: set = set()
        unique = []
        for service in out:
            key = service.url.rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                unique.append(service)
        return unique
