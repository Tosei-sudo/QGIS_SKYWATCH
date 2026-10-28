"""
What the plugin's parts share: the settings file read (and what was wrong
in it), the Esri client and the local GeoPackage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal

from ..core.config import PluginConfig, parse_config_text, read_config_file
from ..core.esri import EsriClient
from ..core.gpkg import GeoPackage, open_local
from . import settings
from .network import get_bytes, qgis_transport
from .tasks import run_task


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
        self.loading = False
        self._gpkg: Optional[GeoPackage] = None

    def reload(self) -> None:
        """
        Reads the settings file again (a URL in a task): the one chosen in the
        settings dialog, else the one shipped in the plugin's folder; the
        defaults when there is neither.
        """
        source, bundled = settings.effective_config_source()
        self.source = source
        self.bundled = bundled
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
        """Every Esri service of the settings file, for choosing their authentication."""
        c = self.config
        out: list[Service] = []
        out += [Service("画像カタログ", x.label, x.url, bool(x.token)) for x in c.image_catalogs]
        out += [Service("衛星カタログ", x.label, x.url, bool(x.token)) for x in c.satellite_catalogs]
        out += [Service("センサーカタログ", x.label, x.url, bool(x.token)) for x in c.sensor_catalogs if x.url]
        out += [Service("計画の保存先", x.label, x.url, bool(x.token)) for x in c.plan_stores]
        for x in c.observation_stores:
            out.append(Service("観測記録の保存先", x.label, x.url, bool(x.token)))
            if x.areas:
                out.append(Service("確認範囲", x.label, x.areas.url, bool(x.areas.token)))
            if x.taxonomy_table:
                out.append(Service("分類体系", x.label, x.taxonomy_table.url, bool(x.taxonomy_table.token)))
        # Base maps on an ArcGIS server (their token is not put in the layer: an authentication configuration is).
        out += [Service("背景地図", x.label, x.url.rstrip("/"), bool(x.token)) for x in c.base_maps if x.type == "esri"]
        return out
