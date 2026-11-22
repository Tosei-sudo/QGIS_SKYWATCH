"""
分類体系の取り込み（表から）の読み込み (core/obs/taxonomy_import.py): a CSV
file (UTF-8 or Shift_JIS, as 観測記録の一括取込み reads it), a sheet of an
Excel or LibreOffice file (GDAL: the first row the columns' names, every
cell as text), or a layer of the project (its attribute table).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Optional

from ..core.obs.api import record_import
from ..core.obs.api import taxonomy_import as ti

#: The files read as a spreadsheet (a sheet chosen).
SHEET_FILES = (".xlsx", ".xls", ".ods")
#: The file dialog's filter.
FILE_FILTER = "表（*.csv *.txt *.tsv *.xlsx *.xls *.ods）;;CSV（*.csv *.txt *.tsv）;;Excel（*.xlsx *.xls）;;すべてのファイル（*）"
#: GDAL's settings while a sheet is read: the first row is the columns' names, every cell text (221, not 221.0).
_SHEET_OPTIONS = {"OGR_XLSX_HEADERS": "FORCE", "OGR_XLSX_FIELD_TYPES": "STRING", "OGR_ODS_HEADERS": "FORCE",
                  "OGR_ODS_FIELD_TYPES": "STRING", "OGR_XLS_HEADERS": "FORCE"}


def is_sheet_file(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in SHEET_FILES


@contextmanager
def _sheet_options():
    """GDAL's settings for reading a sheet, and its errors raised, only meanwhile (QGIS and other plugins share GDAL's state)."""
    from osgeo import gdal

    manager = getattr(gdal, "ExceptionMgr", None)  # GDAL 3.7+
    with (manager(useExceptions=True) if manager is not None else _nothing()):
        was = {k: gdal.GetConfigOption(k) for k in _SHEET_OPTIONS}
        for k, v in _SHEET_OPTIONS.items():
            gdal.SetConfigOption(k, v)
        try:
            yield
        finally:
            for k, v in was.items():
                gdal.SetConfigOption(k, v)


@contextmanager
def _nothing():
    yield


def _open(path: str):
    from osgeo import gdal

    try:
        return gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
    except RuntimeError as error:
        raise ValueError(f"表として開けません: {str(error).strip() or path}") from None


def sheets(path: str) -> list[str]:
    """The sheets of a spreadsheet file."""
    with _sheet_options():
        ds = _open(path)
        if ds is None:
            raise ValueError("表として開けません")
        return [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]


def read_sheet(path: str, sheet: Optional[str] = None) -> tuple[list[str], list[ti.TableRow]]:
    """A sheet's columns and rows (numbered as the sheet's rows: the first under the names is 2)."""
    with _sheet_options():
        ds = _open(path)
        if ds is None or ds.GetLayerCount() == 0:
            raise ValueError("表として開けません")
        layer = ds.GetLayerByName(sheet) if sheet else ds.GetLayer(0)
        if layer is None:
            raise ValueError(f"シート「{sheet}」がありません")
        defn = layer.GetLayerDefn()
        columns = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
        rows = []
        layer.ResetReading()
        for n, f in enumerate(layer, start=2):
            fid = f.GetFID()
            values: dict[str, Any] = {c: (f.GetFieldAsString(i) if f.IsFieldSetAndNotNull(i) else None) for i, c in enumerate(columns)}
            # XLSX / ODS number a feature by its row (the names' row is 1); others in order.
            rows.append(ti.TableRow(int(fid) if fid is not None and fid >= 2 else n, values))
        return columns, rows


def read_file(path: str, sheet: Optional[str] = None) -> tuple[list[str], list[Any]]:
    """A file's columns and rows: a spreadsheet's sheet, else a CSV."""
    if is_sheet_file(path):
        return read_sheet(path, sheet)
    return record_import.read_csv(path)


def layer_table(layer, selected_only: bool = False) -> tuple[list[str], list[Any]]:
    """A layer's attribute names and its features as rows (numbered by feature id)."""
    from .record_import_io import layer_rows

    return layer_rows(layer, selected_only)


#: The backups kept (the newest; older ones removed when one is made).
BACKUPS_KEPT = 20


def backup_folder() -> str:
    """Where the classification is kept before an import: the profile's mikage/taxonomy-backups (練習モード: the practice's folder)."""
    from . import training_io
    from .settings import profile_folder

    session = training_io.active()
    root = session.files.root if session is not None else profile_folder()
    return os.path.join(root, "taxonomy-backups")


def write_backup(text: str, folder: Optional[str] = None) -> str:
    """Keeps the classification (its JSON) as it was before an import; where. Raises OSError when it can't be written."""
    folder = folder or backup_folder()
    os.makedirs(folder, exist_ok=True)
    base = ti.backup_name()[:-len(".json")]
    path = os.path.join(folder, base + ".json")
    n = 2
    while os.path.exists(path):  # two in the same second
        path = os.path.join(folder, f"{base}_{n}.json")
        n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    kept = sorted((e for e in os.listdir(folder) if e.endswith(".json")), key=lambda e: os.path.getmtime(os.path.join(folder, e)))
    for old in kept[:-BACKUPS_KEPT]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    return path


def read_source(source: Any, sheet: Optional[str] = None, selected_only: bool = False) -> tuple[list[str], list[Any], str]:
    """A table for scripts: a file's path (CSV, a sheet), a layer, or a layer's id or name in the project. Its columns, rows and name."""
    from qgis.core import QgsProject, QgsVectorLayer

    if isinstance(source, QgsVectorLayer):
        columns, rows = layer_table(source, selected_only)
        return columns, rows, source.name()
    text = str(source or "")
    if text and os.path.isfile(text):
        columns, rows = read_file(text, sheet)
        return columns, rows, os.path.basename(text)
    project = QgsProject.instance()
    layer = project.mapLayer(text) or next(iter(project.mapLayersByName(text)), None)
    if isinstance(layer, QgsVectorLayer):
        columns, rows = layer_table(layer, selected_only)
        return columns, rows, layer.name()
    raise ValueError(f"表「{text}」なし（ファイルのパス、レイヤー、レイヤーの ID か名前）")
