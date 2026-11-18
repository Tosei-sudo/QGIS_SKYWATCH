"""
地図の描画を速くする設定 (core/raster_speed.py has what and why, with the
measurements).

GDAL's two (GDAL_NUM_THREADS, the block cache) are set in this QGIS only:
when the plugin is loaded, before any image is opened (with the settings
file's `rasterSpeed` as last read), again whenever the settings file is read,
and put back when the plugin is unloaded. Nothing is written anywhere, and a
value set by someone else (the environment, QGIS's 環境変数, another plugin)
is left alone.

QGIS's own options are looked at once in a profile, after QGIS has started
and the settings file is read: those slower than QGIS's defaults are
changed (QgsSettings, and the open map canvases right away), and what they
were is kept so that 「元に戻す」 can put them back (only those not changed
again since).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable, Optional

from osgeo import gdal
from qgis.core import QgsApplication, QgsSettings

from ..core.raster_speed import (
    DEFAULT_RASTER_SPEED, EARLY_RESAMPLING, MAX_THREADS, PARALLEL, QGIS_KEYS, RENDER_CACHE, RasterSpeedOptions, Change,
    as_bool, as_int, cache_for, qgis_changes, shown, threads_for,
)
from . import fast_draw, settings

THREADS = "GDAL_NUM_THREADS"
CACHE = "GDAL_CACHEMAX"
MB = 1048576
#: The person turned it off in 「地図の描画の速さ」 ("1").
OFF_KEY = "rasterSpeed/off"
#: The settings file's `rasterSpeed` as last read (JSON), for the next start (read before the file is).
OPTIONS_KEY = "rasterSpeed/options"
#: QGIS's options were looked at in this profile ("1").
DONE_KEY = "rasterSpeed/qgisDone"
#: QGIS's options changed and what they were (JSON list of Change.to_json), for 「元に戻す」.
CHANGED_KEY = "rasterSpeed/qgisChanged"

#: Off in the tests (each worker would decode on cores the others use; tests/test_qgis_raster_speed.py turns it on).
ENABLED = True

#: What this plugin set in GDAL: THREADS → (the option before, ours), CACHE → (bytes before, ours).
_ours: dict = {}
#: What was left as someone else set it: name → their value.
_theirs: dict = {}


def cores() -> int:
    return max(1, os.cpu_count() or 1)


def ram_mb() -> Optional[int]:
    """The RAM GDAL may use (its own measure, MB), None when unknown."""
    try:
        n = int(gdal.GetUsablePhysicalRAM())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    return n // MB if n > 0 else None


def gdal_version() -> int:
    try:
        return int(gdal.VersionInfo())
    except (TypeError, ValueError):
        return 0


def personal_off() -> bool:
    return settings._get(OFF_KEY) == "1"


def set_personal_off(off: bool) -> None:
    settings._set(OFF_KEY, "1" if off else "0")


def last_options() -> RasterSpeedOptions:
    """The settings file's `rasterSpeed` as last read (the defaults before any)."""
    text = settings._get(OPTIONS_KEY)
    if not text:
        return DEFAULT_RASTER_SPEED
    try:
        return RasterSpeedOptions.from_json(json.loads(text))
    except ValueError:
        return DEFAULT_RASTER_SPEED


def remember_options(options: RasterSpeedOptions) -> None:
    settings._set(OPTIONS_KEY, json.dumps(options.to_json()))


def in_use(options: Optional[RasterSpeedOptions] = None) -> RasterSpeedOptions:
    """The options that apply: off when the person turned it off (or in the tests)."""
    options = options or last_options()
    return RasterSpeedOptions(enabled=False) if (personal_off() or not ENABLED) else options


# ---- GDAL, in this QGIS ----------------------------------------------------------------------------------

def apply_gdal(options: Optional[RasterSpeedOptions] = None) -> None:
    """GDAL_NUM_THREADS and the block cache for `options` (the last read when None); never raises."""
    try:
        options = in_use(options)
        _threads(threads_for(options, cores(), gdal_version()))
        own = _ours[CACHE][0] // MB if CACHE in _ours else gdal.GetCacheMax() // MB
        _cache(cache_for(options, ram_mb(), own))
    except Exception:  # noqa: BLE001 (never keeps QGIS or the plugin from starting)
        pass


def _threads(n: Optional[int]) -> None:
    if n is None:
        _undo(THREADS)
        return
    if THREADS not in _ours:
        now = gdal.GetConfigOption(THREADS)
        if now is not None:
            _theirs[THREADS] = now
            return
        _ours[THREADS] = (None, str(n))
    else:
        _ours[THREADS] = (_ours[THREADS][0], str(n))
    gdal.SetConfigOption(THREADS, str(n))


def _cache(mb: Optional[int]) -> None:
    if mb is None:
        _undo(CACHE)
        return
    if CACHE not in _ours:
        now = os.environ.get(CACHE) or gdal.GetConfigOption(CACHE)
        if now:
            _theirs[CACHE] = now
            return
        _ours[CACHE] = (gdal.GetCacheMax(), mb * MB)
    else:
        _ours[CACHE] = (_ours[CACHE][0], mb * MB)
    gdal.SetCacheMax(mb * MB)


def _undo(name: str) -> None:
    if name not in _ours:
        return
    before, _ = _ours.pop(name)
    if name == THREADS:
        gdal.SetConfigOption(THREADS, before)
    else:
        gdal.SetCacheMax(before)


def revert_gdal() -> None:
    """GDAL as it was before the plugin set it (the plugin unloaded or turned off); never raises."""
    for name in (THREADS, CACHE):
        try:
            _undo(name)
        except Exception:  # noqa: BLE001
            pass


def gdal_set() -> dict:
    """What the plugin set in GDAL now: THREADS → its value, CACHE → MB (and their before: `before_<name>`)."""
    out = {}
    if THREADS in _ours:
        out[THREADS] = _ours[THREADS][1]
    if CACHE in _ours:
        out[CACHE] = _ours[CACHE][1] // MB
        out["before_" + CACHE] = _ours[CACHE][0] // MB
    return out


# ---- QGIS's own options ----------------------------------------------------------------------------------

def qgis_values() -> dict:
    s = QgsSettings()
    return {key: s.value(key) for key in QGIS_KEYS}


def qgis_changed() -> list[Change]:
    """QGIS's options this plugin changed (and not put back)."""
    try:
        raw = json.loads(settings._get(CHANGED_KEY) or "[]")
    except ValueError:
        return []
    return [c for c in (Change.from_json(v) for v in raw if isinstance(raw, list)) if c is not None]


def _same(key: str, a, b) -> bool:
    if key == MAX_THREADS:
        return as_int(a, -1) == as_int(b, -1)
    return as_bool(a, key != EARLY_RESAMPLING) == as_bool(b, key != EARLY_RESAMPLING)


def _live(key: str, value, canvases: Iterable) -> None:
    """The change on QGIS as it runs (QGIS reads these options when a map canvas is made, the cores at its start)."""
    if key == MAX_THREADS:
        QgsApplication.setMaxThreads(as_int(value, -1))
        return
    for canvas in canvases:
        try:
            if key == PARALLEL:
                canvas.setParallelRenderingEnabled(as_bool(value, True))
            elif key == RENDER_CACHE:
                canvas.setCachingEnabled(as_bool(value, True))
        except RuntimeError:  # a map view closed meanwhile
            pass


def apply_qgis(canvases: Iterable = ()) -> list[Change]:
    """Changes QGIS's options slower than its defaults (core/raster_speed.qgis_changes) and keeps what they were."""
    canvases = list(canvases)
    changes = qgis_changes(qgis_values(), cores())
    s = QgsSettings()
    for change in changes:
        s.setValue(change.key, change.after)
        _live(change.key, change.after, canvases)
    kept = {c.key: c for c in qgis_changed()}
    kept.update({c.key: c for c in changes})
    settings._set(CHANGED_KEY, json.dumps([c.to_json() for c in kept.values()]))
    settings._set(DONE_KEY, "1")
    return changes


def apply_qgis_once(options: Optional[RasterSpeedOptions] = None, canvases: Iterable = ()) -> list[Change]:
    """apply_qgis the first time in this profile (when `qgisSettings` and not turned off); [] after."""
    options = in_use(options)
    if not options.enabled or not options.qgis_settings or settings._get(DONE_KEY) == "1":
        return []
    try:
        return apply_qgis(canvases)
    except Exception:  # noqa: BLE001 (QGIS keeps its options as they were)
        return []


def revert_qgis(canvases: Iterable = ()) -> list[Change]:
    """QGIS's options this plugin changed back to what they were — those not changed again since. The ones put back."""
    canvases = list(canvases)
    s = QgsSettings()
    back = []
    for change in qgis_changed():
        if not _same(change.key, s.value(change.key), change.after):
            continue  # changed since (in QGIS's options): theirs now
        if change.before is None:
            s.remove(change.key)
        else:
            s.setValue(change.key, change.before)
        _live(change.key, change.before, canvases)
        back.append(change)
    settings._set(CHANGED_KEY, "[]")
    return back


# ---- turning it off and on, what is in use ---------------------------------------------------------------

def turn_off(canvases: Iterable = ()) -> list[Change]:
    """「描画を速くする設定を使う」 off: GDAL and QGIS's options as they were; not again at the next start."""
    set_personal_off(True)
    revert_gdal()
    return revert_qgis(canvases)


def turn_on(options: Optional[RasterSpeedOptions] = None, canvases: Iterable = ()) -> list[Change]:
    """On again: GDAL set again, QGIS's options looked at again (those slower than its defaults changed)."""
    set_personal_off(False)
    options = in_use(options)
    apply_gdal(options)
    if not options.enabled or not options.qgis_settings:
        return []
    try:
        return apply_qgis(canvases)
    except Exception:  # noqa: BLE001
        return []


@dataclass(frozen=True)
class Row:
    """One line of 「地図の描画の速さ」: what, its value now, and who set it."""
    what: str
    now: str
    state: str


def rows(options: Optional[RasterSpeedOptions] = None) -> list[Row]:
    options = options or last_options()
    out = []
    ours = gdal_set()
    now = gdal.GetConfigOption(THREADS)
    if THREADS in ours:
        state = f"MIKAGE が設定（この PC の {cores()} コアから 1 つ残す）"
    elif THREADS in _theirs:
        state = "環境変数などの値のまま"
    elif threads_for(RasterSpeedOptions(), cores(), gdal_version()) is None:
        state = "変えない（" + ("GDAL が 3.6 より前" if gdal_version() < 3060000 else "コアが 2 つ以下") + "）"
    else:
        state = "変えていない"
    out.append(Row("GDAL の読み込みのスレッド（GDAL_NUM_THREADS）", now or "なし（1 コアで展開）", state))
    mb = gdal.GetCacheMax() // MB
    if CACHE in ours:
        state = f"MIKAGE が設定（GDAL の既定 {ours['before_' + CACHE]:,} MB）"
    elif CACHE in _theirs:
        state = "環境変数などの値のまま"
    else:
        state = "GDAL の既定のまま"
    out.append(Row("GDAL のブロックキャッシュ（GDAL_CACHEMAX）", f"{mb:,} MB", state))
    changed = {c.key: c for c in qgis_changed()}
    values = qgis_values()
    for key in (PARALLEL, MAX_THREADS, RENDER_CACHE, EARLY_RESAMPLING):
        c = changed.get(key)
        if c is not None and _same(key, values.get(key), c.after):
            state = f"MIKAGE が変更（元: {shown(key, c.before)}）"
        else:
            state = "変えていない"
        out.append(Row(f"QGIS: {c.label if c else Change(key, None, None).label}", shown(key, values.get(key)), state))
    return out


def summary() -> str:
    """One line for the settings dialog and the log."""
    ours = gdal_set()
    parts = []
    threads = gdal.GetConfigOption(THREADS)
    parts.append(f"GDAL のスレッド {threads}" if threads else "GDAL のスレッドなし")
    parts.append(f"ブロックキャッシュ {gdal.GetCacheMax() // MB:,} MB" + (f"（既定 {ours['before_' + CACHE]:,} MB）" if CACHE in ours else ""))
    changed = qgis_changed()
    parts.append(f"QGIS の設定の変更 {len(changed)} 件")
    fast = "、MIKAGE の速い描画（試作）" if fast_draw.on() else ""
    if personal_off():
        return "使わない（" + "、".join(parts) + "）" + fast
    return "、".join(parts) + fast
