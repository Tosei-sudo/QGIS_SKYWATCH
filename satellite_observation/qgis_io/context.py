"""
What the plugin's parts share: the settings file read (and what was wrong
in it), the Esri client and the local GeoPackage.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal

from ..core.config import PluginConfig, parse_config_text, read_config_file
from ..core.esri import EsriClient
from ..core.gp import service_root as gp_service_root
from ..core.gpkg import GeoPackage, open_local
from ..core.plan.weather import DEFAULT_CLOUD_FORECAST
from ..core.plan.names import NO_NAMES, SatelliteNames, load_satellite_names
from ..core.edit_stamp import set_tracking
from ..core.time_zone import set_rules
from . import settings
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
    #: Whether the settings file writes a token for it (the token itself is not kept here).
    has_token: bool


class PluginContext(QObject):
    """The settings file and the connections, shared by the docks."""

    #: The settings file was read again.
    configChanged = pyqtSignal()

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
        self._gpkg: Optional[GeoPackage] = None
        #: The satellite name tables (`satelliteAliases`) once read; None until then (read again after a reload).
        self._names: Optional[SatelliteNames] = None
        self._names_lock = threading.Lock()
        self.configChanged.connect(self._forget_names)
        # First of all who hear it: every request after a reload reads dates by the new file's time zones.
        self.configChanged.connect(lambda: set_rules(self.config.time_zones))
        self.configChanged.connect(lambda: set_tracking(self.config.edit_tracking))

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
        if not source:
            self.config, self.problems = PluginConfig(), []
            self.configChanged.emit()
            return
        if re.match(r"^https?://", source, re.IGNORECASE):
            self.loading = True

            def work(task):
                return get_bytes(source, settings.auth_for(source)).decode("utf-8-sig", errors="replace")

            def done(text):
                self.loading = False
                self.config, self.problems = parse_config_text(text)
                self.configChanged.emit()

            def failed(message):
                self.loading = False
                self.config, self.problems = PluginConfig(), [f"設定ファイルの取得不可（{message}）"]
                self.configChanged.emit()

            run_task("設定ファイルの読み込み", work, done, failed)
            return
        self.config, self.problems = read_config_file(source)
        self.configChanged.emit()

    def esri(self) -> EsriClient:
        """An Esri client whose requests follow QGIS's proxy and the authcfg chosen per service (use inside a task)."""
        return EsriClient(qgis_transport(settings.auth_for))

    @staticmethod
    def token_for(url: str, token: Optional[str]) -> Optional[str]:
        """The settings file's token for a service, unless an authentication configuration is chosen for it."""
        return None if settings.auth_for(url) else token

    def _forget_names(self) -> None:
        with self._names_lock:
            self._names = None

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
            names, problems = load_satellite_names(client or self.esri(), tables, self.token_for, cancelled)
        except Exception:  # noqa: BLE001 (names only make matches better; the work goes on without them)
            return NO_NAMES
        with self._names_lock:
            # Kept only when every table was read (one that failed is asked again next time).
            if not problems and self.config.satellite_aliases is tables:
                self._names = names
        return names

    def known_names(self) -> SatelliteNames:
        """The other names already read (for the main thread, which does not wait for the tables)."""
        with self._names_lock:
            return self._names or NO_NAMES

    def gpkg(self) -> GeoPackage:
        """The local GeoPackage (opened once)."""
        if self._gpkg is None:
            self._gpkg = open_local(settings.local_gpkg_path())
        return self._gpkg

    def close(self) -> None:
        if self._gpkg is not None:
            self._gpkg.close()
            self._gpkg = None

    def services(self) -> list[Service]:
        """Every service of the settings file that may need a sign-in, for choosing their authentication."""
        c = self.config
        out: list[Service] = []
        out += [Service("画像カタログ", x.label, x.url, bool(x.token)) for x in c.image_catalogs]
        out += [Service("衛星カタログ", x.label, x.url, bool(x.token)) for x in c.satellite_catalogs]
        out += [Service("センサーカタログ", x.label, x.url, bool(x.token)) for x in c.sensor_catalogs if x.url]
        out += [Service("衛星名の対応表", x.label, x.url, bool(x.token)) for x in c.satellite_aliases]
        out += [Service("計画の保存先", x.label, x.url, bool(x.token)) for x in c.plan_stores]
        for x in c.observation_stores:
            out.append(Service("観測記録の保存先", x.label, x.url, bool(x.token)))
            if x.areas:
                out.append(Service("確認範囲", x.label, x.areas.url, bool(x.areas.token)))
            if x.taxonomy_table:
                out.append(Service("分類体系", x.label, x.taxonomy_table.url, bool(x.taxonomy_table.token)))
        # Base maps on an ArcGIS server (their token is not put in the layer: an authentication configuration is).
        out += [Service("背景地図", x.label, x.url.rstrip("/"), bool(x.token)) for x in c.base_maps if x.type == "esri"]
        # Processing services, under the URL their sign-in is kept for (the GPServer, or the OGC API landing page).
        out += [Service("サーバ処理", x.label, gp_service_root(x.url), bool(x.token)) for x in c.gp_services]
        # The request ledger and its history, AI results, the weather and the rest (#75).
        for x in c.request_stores:
            out.append(Service("撮像依頼台帳", x.label, x.url, bool(x.token)))
            if x.history:
                out.append(Service("出力履歴", x.label, x.history, bool(x.token)))
        out += [Service("AI 結果の送信先", x.label, x.url, bool(x.token)) for x in c.detection_outputs]
        out += [Service("外部AI結果", x.label, x.url, bool(x.token)) for x in c.ai_result_sources]
        if c.cloud_forecast is not None and _web(getattr(c.cloud_forecast, "url", "")) and c.cloud_forecast.url != DEFAULT_CLOUD_FORECAST.url:
            out.append(Service("雲量予報", c.cloud_forecast.label, c.cloud_forecast.url, False))
        out += [Service("移動体の情報源", x.label, x.url, bool(getattr(x, "token", None))) for x in c.moving_feeds if _web(x.url)]
        out += [Service("地名検索", x.label, x.url, bool(x.token)) for x in c.geocoders if _web(x.url)]
        out += [Service("起動時レイヤー", x.layer or x.url, x.url, bool(getattr(x, "token", None))) for x in c.startup_layers if _web(x.url)]
        for x in c.ai_models:
            for url in (getattr(x, name, "") for name in ("url", "encoder", "decoder")):
                if _web(url):
                    out.append(Service("AI モデル", x.label, url, False))
        # One row per URL: the first role that names it.
        seen: set = set()
        unique = []
        for service in out:
            key = service.url.rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                unique.append(service)
        return unique
