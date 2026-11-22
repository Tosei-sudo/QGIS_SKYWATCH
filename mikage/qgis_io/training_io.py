"""
練習モード in QGIS (core/training): where its files are, whether it is on,
the profile's settings kept aside meanwhile, and its server.

While it is on (`active()`), qgis_io/settings.py answers with the practice's
files: its settings file (written here, every service the practice server
on 127.0.0.1), its local GeoPackage, its offline work, its place cache, its
opened images and thumbnails, all under the profile's mikage/training. The
profile's settings that hold data rather than the screen's layout
(SAVED_KEYS: 担当 and whether they wait to be sent to `userRoles`, the
reminders of shots, the request files' folder) are kept aside when it
starts and put back when it ends, and at the next start of the plugin after
QGIS stopped while practising (`recover`): nothing chosen while practising
reaches the production tables.

The masters only read (satellites, the taxonomy, places, …:
core/training/masters.py) may be the production's (`Production`): their
tokens are kept in memory (`TrainingSession.config_text`), the file written
has none.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from qgis.core import QgsApplication, QgsSettings

from ..core.config import load_jsonc
from ..core.training.api import TrainingServer, masters, open_data, training_config

#: Whether practising (set when it starts, taken away when it ends: still there at a start, QGIS stopped while practising).
ACTIVE_KEY = "mikage/training/active"
#: The SAVED_KEYS' values when it started ({key: value or None}).
SAVED_KEY = "mikage/training/saved"
#: The practice server's last port (the data's URLs carry it: the same one again when it is free).
PORT_KEY = "mikage/training/port"
#: The profile's settings that are data (kept aside while practising, put back after).
SAVED_KEYS = ("mikage/roles", "mikage/rolesPending", "mikage/role", "mikage/plan_reminders", "mikage/request_export_folder")
#: A base for the data made before the server has a port (moved to the server's when it starts: core/training/server.rebase).
NO_BASE = "http://127.0.0.1:0"


def folder() -> str:
    """The practice's folder in the QGIS user profile (mikage/training)."""
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "mikage", "training")


@dataclass(frozen=True)
class TrainingFiles:
    """Where the practice keeps what it has (one folder, per QGIS profile: nobody else's practice mixes in)."""
    root: str

    @property
    def server(self) -> str:
        """The practice server's data (mock.sqlite, files/catalog/*.tif, basemap.tif)."""
        return os.path.join(self.root, "server")

    @property
    def config(self) -> str:
        return os.path.join(self.root, "config.training.jsonc")

    @property
    def gpkg(self) -> str:
        return os.path.join(self.root, "observations.gpkg")

    @property
    def offline(self) -> str:
        return os.path.join(self.root, "offline_work.sqlite")

    @property
    def place_cache(self) -> str:
        return os.path.join(self.root, "place_cache.sqlite")

    @property
    def opened_images(self) -> str:
        return os.path.join(self.root, "opened_images.json")

    @property
    def thumbnails(self) -> str:
        return os.path.join(self.root, "thumbnails")

    @property
    def outputs(self) -> str:
        """Where the practice's files go (撮像計画ファイル, 通報, the exports chosen): never a production folder."""
        return os.path.join(self.root, "出力")

    @property
    def basemap(self) -> str:
        return os.path.join(self.server, "basemap.tif")

    @property
    def progress(self) -> str:
        """チュートリアル's progress (kept when the practice data is made again)."""
        return os.path.join(self.root, "progress.json")

    @property
    def sample_images(self) -> str:
        """The sample's images as made (ids, times, ships: what チュートリアル points at)."""
        return os.path.join(self.server, "files", "catalog", "images.json")

    @property
    def images_drawn(self) -> str:
        """Written once the sample images are drawn as images (not the server's cartoons)."""
        return os.path.join(self.server, "images.drawn")

    def ready(self) -> bool:
        """Whether the sample is there (made the first time 練習モード starts)."""
        return all(os.path.exists(p) for p in (os.path.join(self.server, "mock.sqlite"), self.basemap, self.images_drawn))

    def created(self) -> Optional[int]:
        """When the sample was made (ms), None when it is not there."""
        path = self.sample_images
        try:
            return int(os.path.getmtime(path) * 1000)
        except OSError:
            return None


def files() -> TrainingFiles:
    return TrainingFiles(folder())


@dataclass
class TrainingSession:
    """練習モード while it is on."""
    files: TrainingFiles
    #: The settings file read while practising: the one written here, or the organisation's (`training.config`).
    config_source: str
    #: Whether it is the built-in sample on the plugin's own server.
    builtin: bool = True
    server: Optional[TrainingServer] = None
    started: float = field(default_factory=time.time)
    #: The practice's settings file as read while practising (with the production's tokens; None: the file's own text).
    config_text: Optional[str] = None
    #: The production's settings file, as it was when practising started (its masters read meanwhile).
    production: Optional["Production"] = None
    #: The masters taken from it (core/training/settings.py's MASTERS' keys).
    from_production: tuple = ()

    @property
    def base(self) -> str:
        return self.server.base if self.server is not None else ""


_session: Optional[TrainingSession] = None


def active() -> Optional[TrainingSession]:
    """練習モード, while it is on (None in production)."""
    return _session


# ---- the sample ------------------------------------------------------------------------------------------


def prepare(f: TrainingFiles, reset: bool = False, cancelled: Callable[[], bool] = lambda: False,
            log: Callable[[str], None] = lambda _text: None) -> None:
    """
    The sample made (in a task: drawing its images takes a few seconds):
    the server's data and the images drawn over its cartoons, and the
    background map. `reset`: everything of the practice thrown away first
    (what was recorded while practising too).
    """
    if reset:
        clear(f)
    os.makedirs(f.root, exist_ok=True)
    store = open_data(f.server, NO_BASE, log=log)
    store.close()
    if cancelled():
        return
    from .training_images import draw_images

    draw_images(f.server, log=log, cancelled=cancelled)
    if cancelled():
        return
    with open(f.images_drawn, "w", encoding="utf-8") as out:
        out.write(time.strftime("%Y-%m-%d %H:%M:%S"))


def clear(f: TrainingFiles) -> None:
    """Everything of the practice thrown away (its data, its local records, its outputs); not its tutorial's progress."""
    keep = {os.path.basename(f.progress)}
    if not os.path.isdir(f.root):
        return
    for name in os.listdir(f.root):
        if name in keep:
            continue
        path = os.path.join(f.root, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


# ---- starting and ending ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Production:
    """The production's settings file (its value, where it was read from) and the masters to take from it."""
    value: dict
    source: str
    names: tuple


def write_config(f: TrainingFiles, base: str, files: str, production: Optional[Production] = None) -> tuple[Optional[str], tuple]:
    """
    The practice's settings file written for the server at `base`: its text
    with the production's tokens (None when it is the file's own) and the
    masters taken from the production.
    """
    text = training_config(base, files, f.outputs)
    taken: list = []
    whole: Optional[str] = None
    if production is not None and production.names:
        value, taken = masters.with_production(load_jsonc(text), production.value, production.names,
                                               production.source, os.path.join(f.outputs, "撮像計画ファイル"))
        if taken:
            head = ("練習モードの設定ファイル（MIKAGE が練習を始めるたびに書き出す。手で直しても次に上書きされる）\n"
                    f"書き込む表はすべてこの PC の中の練習用サーバー。{masters.names_text(taken)}は本番の設定ファイルから（読むだけ）\n"
                    "本番のトークンはここには書かない（練習中は MIKAGE が覚えている）")
            whole = masters.config_text(value, head)
            text = masters.config_text(masters.without_tokens(value), head)
    with open(f.config, "w", encoding="utf-8") as out:
        out.write(text)
    return (whole if whole != text else None), tuple(taken)


def start_server(f: TrainingFiles, production: Optional[Production] = None) -> tuple[TrainingServer, Optional[str], tuple]:
    """
    The practice server started (on its last port when it is free) and the
    practice's settings file written for it (write_config: also its text with
    the production's tokens, and the masters taken from the production).
    """
    s = QgsSettings()
    try:
        port = int(s.value(PORT_KEY, 0) or 0)
    except (TypeError, ValueError):
        port = 0
    server = TrainingServer(f.server)
    base = server.start(port)
    s.setValue(PORT_KEY, server.port)
    os.makedirs(f.outputs, exist_ok=True)
    text, taken = write_config(f, base, server.files, production)
    return server, text, taken


def begin(session: TrainingSession) -> None:
    """練習モード on: the profile's data settings kept aside (and kept aside for a start after QGIS stopped meanwhile)."""
    global _session
    s = QgsSettings()
    if s.value(ACTIVE_KEY, "") != "1":
        saved = {key: (s.value(key) if s.contains(key) else None) for key in SAVED_KEYS}
        s.setValue(SAVED_KEY, json.dumps({k: (v if v is None or isinstance(v, (str, int, float, bool)) else str(v))
                                          for k, v in saved.items()}, ensure_ascii=False))
        s.setValue(ACTIVE_KEY, "1")
        s.sync()
    _session = session


def end() -> Optional[TrainingSession]:
    """練習モード off: the profile's data settings back as they were, the server stopped; the session that ended."""
    global _session
    session, _session = _session, None
    if session is not None and session.server is not None:
        session.server.stop()
    put_back()
    return session


def put_back() -> bool:
    """The SAVED_KEYS back as before practising (False: nothing kept aside)."""
    s = QgsSettings()
    if s.value(ACTIVE_KEY, "") != "1":
        return False
    try:
        saved = json.loads(s.value(SAVED_KEY, "{}") or "{}")
    except ValueError:
        saved = {}
    for key in SAVED_KEYS:
        value = saved.get(key) if isinstance(saved, dict) else None
        if value is None:
            s.remove(key)
        else:
            s.setValue(key, value)
    s.remove(SAVED_KEY)
    s.remove(ACTIVE_KEY)
    s.sync()
    return True


def recover() -> bool:
    """At the plugin's start: QGIS stopped while practising, so what was kept aside comes back (True when it did)."""
    return put_back()
