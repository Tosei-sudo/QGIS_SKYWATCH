"""
地図の描画を速くする設定 (`rasterSpeed`). tsudo, 2026-11-18: 「全体的にラスター
処理のパフォーマンスを上げることって、プラグインからできる？ もしくは設定変更
とかあればその提案を。設定変更でも、プラグインインストール時にプラグイン側から
変更できるならやってほしい」 (「描画のことを特に指してる」).

What the plugin changes, as measured on a 16000² 3-band 16-bit DEFLATE GeoTIFF
with an .ovr (QGIS 3.34, GDAL 3.8, 4 cores, a 1700×1073 map):

- GDAL_NUM_THREADS, in this QGIS only, at each start: GDAL 3.6 and later
  decode the tiles or strips one read covers on several cores. A new area of
  a tiled image 37–47 → 25–34 ms (the first view 90 → 40 ms); of a striped
  one (each strip the whole width) 270–530 → 95–170 ms. An area read just
  before is a few ms slower (13 → 17–29 ms), those reads going round part of
  GDAL's block cache; the PC's cores less one, so one stays for the screen.
- GDAL's block cache (GDAL_CACHEMAX), in this QGIS only: GDAL keeps the
  decoded blocks up to 5 % of the RAM by itself, and an area pushed out
  costs its decoding again (≈ 30 ms a screen, the first rows of a striped
  image 0.5 s). 10 % of the RAM, at most 2 GB, never less than GDAL's own.
- QGIS's own options, only those slower than QGIS's defaults, once in a
  profile, with 「元に戻す」: parallel rendering (two images: 9.5 → 6.5 s
  over the bench), all the cores, the render cache, and early (GDAL's)
  resampling when the default resampling is bilinear or cubic (zoomed out,
  bilinear with QGIS's oversampling 2: 577 → 165 ms).

An environment variable already set (by an administrator, or in QGIS's
設定 > オプション > システム > 環境変数) is left as it is: theirs wins.
Everything QGIS spends besides is its own colouring of the pixels (≈ 90 of
the ≈ 120 ms a 16-bit image takes), which no setting changes; MANUAL lists
what only a person (or the data's side) can do.

    "rasterSpeed": false                       // the plugin changes nothing
    "rasterSpeed": {"gdalThreads": "auto",     // a number of cores, or false (not changed)
                    "gdalCacheMB": "auto",     // MB, or false (not changed)
                    "qgisSettings": true}      // false: QGIS's options are never changed
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: The most the block cache is raised to by itself (MB), and its share of the RAM.
CACHE_CAP_MB = 2048
CACHE_SHARE = 0.10
#: An explicit size is never more than this share of the RAM.
CACHE_MOST_SHARE = 0.5
#: GDAL decodes one read on several cores from this version (GTiff's multi-threaded reading).
THREADED_READS = 3060000


@dataclass(frozen=True)
class RasterSpeedOptions:
    #: Whether the plugin changes anything at all.
    enabled: bool = True
    #: GDAL_NUM_THREADS: 0 the PC's cores less one ("auto"), a number that many, None not changed.
    gdal_threads: Optional[int] = 0
    #: GDAL's block cache in MB: 0 its own share of the RAM ("auto"), a number that size, None not changed.
    gdal_cache_mb: Optional[int] = 0
    #: QGIS's own options (parallel rendering, cores, render cache, early resampling), put right once in a profile.
    qgis_settings: bool = True

    def to_json(self) -> dict:
        return {"enabled": self.enabled, "gdalThreads": self.gdal_threads, "gdalCacheMB": self.gdal_cache_mb,
                "qgisSettings": self.qgis_settings}

    @staticmethod
    def from_json(value: Any) -> "RasterSpeedOptions":
        """Back from to_json (the plugin's own copy of the last settings file read); the defaults for anything else."""
        if not isinstance(value, dict):
            return DEFAULT_RASTER_SPEED

        def count(v):
            return int(v) if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

        return RasterSpeedOptions(value.get("enabled") is not False, count(value.get("gdalThreads")),
                                  count(value.get("gdalCacheMB")), value.get("qgisSettings") is not False)


DEFAULT_RASTER_SPEED = RasterSpeedOptions()


def raster_speed_of(value: Any, problems: list[str]) -> RasterSpeedOptions:
    """The settings file's `rasterSpeed` (true / false / an object); the defaults, with the reason in `problems`, for what cannot be read."""
    if isinstance(value, bool):
        return RasterSpeedOptions(enabled=value)
    if not isinstance(value, dict):
        problems.append("rasterSpeed は true・false かオブジェクトで指定")
        return DEFAULT_RASTER_SPEED

    def count(key: str, what: str) -> Optional[int]:
        if key not in value:
            return 0
        v = value[key]
        if isinstance(v, str):  # as the browser config maker writes a text field: "auto", "3", "false"
            text = v.strip().lower()
            if text == "auto":
                return 0
            if text == "false":
                return None
            if text.isdigit():
                v = int(text)
        if v is False or v is None:
            return None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 1 and v == int(v):
            return int(v)
        problems.append(f"rasterSpeed.{key} は \"auto\"・{what}（1 以上の整数）か false（変えない）で指定")
        return 0

    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append("rasterSpeed.enabled は true か false で指定")
        enabled = True
    qgis = value.get("qgisSettings", True)
    if not isinstance(qgis, bool):
        problems.append("rasterSpeed.qgisSettings は true か false で指定")
        qgis = True
    return RasterSpeedOptions(enabled, count("gdalThreads", "コア数"), count("gdalCacheMB", "MB"), qgis)


def threads_for(options: RasterSpeedOptions, cores: int, gdal_version: int) -> Optional[int]:
    """The GDAL_NUM_THREADS to set (None: none): the PC's cores less one, or the settings file's number (at most the cores)."""
    if not options.enabled or options.gdal_threads is None or gdal_version < THREADED_READS:
        return None
    cores = max(1, cores)
    n = min(options.gdal_threads, cores) if options.gdal_threads else cores - 1
    return n if n >= 2 else None


def cache_for(options: RasterSpeedOptions, ram_mb: Optional[int], gdal_mb: int) -> Optional[int]:
    """
    The block cache in MB to set (None: as it is). By itself 10 % of the RAM, at most CACHE_CAP_MB, and only when
    more than GDAL's own (`gdal_mb`); the settings file's number as written, but never over half the RAM.
    """
    if not options.enabled or options.gdal_cache_mb is None:
        return None
    if options.gdal_cache_mb:
        size = options.gdal_cache_mb
        if ram_mb:
            size = min(size, int(ram_mb * CACHE_MOST_SHARE))
        return size if size != gdal_mb else None
    if not ram_mb:
        return None
    size = min(int(ram_mb * CACHE_SHARE), CACHE_CAP_MB)
    return size if size > gdal_mb else None


# ---- QGIS's own options ----------------------------------------------------------------------------------

PARALLEL = "qgis/parallel_rendering"
MAX_THREADS = "qgis/max_threads"
RENDER_CACHE = "qgis/enable_render_caching"
EARLY_RESAMPLING = "raster/default-early-resampling"
ZOOMED_IN = "Raster/defaultZoomedInResampling"
ZOOMED_OUT = "Raster/defaultZoomedOutResampling"
#: QGIS's options read to decide (the resampling ones only read).
QGIS_KEYS = (PARALLEL, MAX_THREADS, RENDER_CACHE, EARLY_RESAMPLING, ZOOMED_IN, ZOOMED_OUT)
#: How QGIS's options dialog names them (設定 > オプション > レンダリング).
LABELS = {
    PARALLEL: "複数の CPU コアで並列にレイヤーを描画",
    MAX_THREADS: "描画に使うコアの最大数",
    RENDER_CACHE: "可能であれば描画キャッシュを使う",
    EARLY_RESAMPLING: "新しいラスターレイヤーの早期リサンプリング",
}
#: Short names, for the one line of the message bar.
SHORT = {PARALLEL: "並列描画", MAX_THREADS: "描画のコア数", RENDER_CACHE: "描画キャッシュ", EARLY_RESAMPLING: "早期リサンプリング"}
NEAREST = "nearest neighbour"


def as_bool(value: Any, default: bool) -> bool:
    """A QSettings value as a bool (an ini file gives "true" / "false")."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off", ""):
        return False
    return default


def as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip()) if value is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Change:
    """One of QGIS's options changed: its key, what it was (None: never set, QGIS's default) and what it is made."""
    key: str
    before: Any
    after: Any
    #: Why, for the person (the message bar's tooltip and the dialog).
    why: str = ""

    @property
    def label(self) -> str:
        return LABELS.get(self.key, self.key)

    @property
    def short(self) -> str:
        return SHORT.get(self.key, self.key)

    def to_json(self) -> dict:
        return {"key": self.key, "before": self.before, "after": self.after}

    @staticmethod
    def from_json(value: Any) -> Optional["Change"]:
        if not isinstance(value, dict) or not isinstance(value.get("key"), str):
            return None
        return Change(value["key"], value.get("before"), value.get("after"))


def shown(key: str, value: Any) -> str:
    """An option's value as QGIS's options dialog shows it."""
    if key == MAX_THREADS:
        n = as_int(value, -1)
        return "すべて" if n < 1 else f"{n}"
    if key in (PARALLEL, RENDER_CACHE):
        return "オン" if as_bool(value, True) else "オフ"
    if key == EARLY_RESAMPLING:
        return "オン" if as_bool(value, False) else "オフ"
    return str(value)


def qgis_changes(values: Mapping[str, Any], cores: int) -> list[Change]:
    """
    QGIS's options to change, from what they are (`values`: key → its QSettings value, None when never set): only those
    slower than QGIS's own defaults, and early resampling when the default resampling is not nearest neighbour.
    """
    changes = []
    if not as_bool(values.get(PARALLEL), True):
        changes.append(Change(PARALLEL, values.get(PARALLEL), True,
                              "オフだと画像を 1 枚ずつ順に描く（2 枚で約 1.5 倍の時間）"))
    n = as_int(values.get(MAX_THREADS), -1)
    if 1 <= n < max(1, cores):
        changes.append(Change(MAX_THREADS, values.get(MAX_THREADS), -1,
                              f"{n} コアに制限されていた（この PC は {cores} コア）"))
    if not as_bool(values.get(RENDER_CACHE), True):
        changes.append(Change(RENDER_CACHE, values.get(RENDER_CACHE), True,
                              "オフだと 1 枚の画像を変えるたびに全レイヤーを描き直す"))
    smooth = [str(values.get(k) or NEAREST).strip().lower() for k in (ZOOMED_IN, ZOOMED_OUT)]
    if any(s != NEAREST for s in smooth) and not as_bool(values.get(EARLY_RESAMPLING), False):
        changes.append(Change(EARLY_RESAMPLING, values.get(EARLY_RESAMPLING), True,
                              "既定のリサンプリングが最近傍でないので、GDAL が読み込み時に補間（縮小表示が約 3.5 倍速い）"))
    return changes


#: What only a person (or an administrator, or the data's side) can do: (title, what to do and why).
MANUAL = (
    ("画像の形式（データを用意する側）",
     "タイル化した GeoTIFF（COG。圧縮は DEFLATE か ZSTD、内部ピラミッド付き）にする。ストリップ（横一列ずつ）の圧縮 GeoTIFF は、"
     "等倍で初めて見る場所の読み込みが 0.3〜0.5 秒（タイル化なら 0.04〜0.09 秒）。JPEG 2000 は展開が重い。"
     "変換例: gdal_translate -of COG -co COMPRESS=DEFLATE 元.tif 新.tif"),
    ("共有フォルダの画像",
     "読み込みの速さはネットワークで決まる。設定ファイルの overviews.location を \"cache\" にすると、ピラミッド（.ovr）を"
     "この PC に置くので、縮小表示は PC のディスクから読む。大きな画像を長く見るときは PC にコピーして開く"),
    ("ウイルス対策ソフト（管理者）",
     "リアルタイム検査が画像やピラミッドの読み込みのたびに走る。画像のフォルダとピラミッドのキャッシュフォルダ"
     "（QGIS プロファイルの gdal_pam。設定ダイアログの「ピラミッドのキャッシュ」）を検査の除外にする"),
    ("保存済みプロジェクトのレイヤーのリサンプリング",
     "早期リサンプリングは新しく追加するレイヤーから。保存済みのレイヤーで縮小時に「バイリニア」などを使っていれば、"
     "レイヤーのプロパティ > シンボロジ > リサンプリングで「早期リサンプリング」をオン（または「最近傍」）にする"),
    ("16 ビット画像の色付け",
     "残りの時間の多くは QGIS 自身の色付け（1 画面 約 0.09 秒）で、上の設定では変わらない。「画像の色付けを MIKAGE が速く描く"
     "（試作）」をオンにすると、MIKAGE が同じ色で速く描く。「画像補正」ドックで補正をオンにした画像は、もともと MIKAGE の色付けで描く"),
    ("電源の設定",
     "ノート PC は電源につなぐ。Windows の電源モードを「最適なパフォーマンス」（高パフォーマンス）にする"),
    ("値を固定したいとき（管理者）",
     "GDAL_NUM_THREADS・GDAL_CACHEMAX を Windows の環境変数か QGIS の 設定 > オプション > システム > 環境変数 に書くと、"
     "プラグインはその値に従って変えない。設定ファイルの rasterSpeed で全体を止めることもできる"),
)
