"""
Training datasets (教師データセット) of images outside the catalog: a small
JSON file listing image and annotation files by their paths (nothing copied),
grouped by how the annotations are written, for 「AI 学習（物体検出）」 to
train on alongside the catalog's images and observation records.

The annotations read here are those of common detection datasets, in the
image's pixels: YOLO (`class cx cy w h`, fractions of the image; with 8
corner values, YOLO OBB), DOTA (`x1 y1 … x4 y4 class difficult`), Pascal
VOC (XML) and COCO (one JSON for many images). A rotated box trains as the
box around it. Vector files (GeoJSON, GeoPackage, Shapefile…), in map
coordinates, are read through QGIS (qgis_io.dataset_io).

    {"format": "satobs-dataset", "version": 1, "name": …,
     "groups": [{"format": "yolo", "names": ["car", …], "class_map": {"car": "車両"},
                 "items": [{"image": "/data/a.tif", "labels": "/data/a.txt"}, …]}, …]}

A group's `class_map` turns the annotations' class names into kinds of the
classification (分類体系); a name not in it is used as it is (a kind, or a
class' name or other name).
"""
from __future__ import annotations

import json
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

FORMAT = "satobs-dataset"
VERSION = 1
#: Annotation formats: in the image's pixels, and in map coordinates (vector files).
PIXEL_FORMATS = ("yolo", "dota", "voc", "coco")
FORMATS = PIXEL_FORMATS + ("vector",)
IMAGE_EXTENSIONS = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".jp2", ".img", ".bmp", ".vrt", ".ntf", ".nitf")
VECTOR_EXTENSIONS = (".geojson", ".json", ".gpkg", ".shp", ".kml", ".gml", ".fgb", ".sqlite")
#: Files that name YOLO's classes, looked for beside the annotations and above them.
YOLO_NAME_FILES = ("classes.txt", "obj.names", "classes.names", "data.yaml", "dataset.yaml", "data.yml")


class DatasetError(Exception):
    pass


@dataclass
class Item:
    """One image and its annotations (`key`: the image's name in a COCO file)."""
    image: str
    labels: str = ""
    key: str = ""

    def to_json(self) -> dict:
        out = {"image": self.image, "labels": self.labels}
        if self.key:
            out["key"] = self.key
        return out


@dataclass
class Group:
    """Images annotated the same way."""
    format: str
    items: list[Item] = field(default_factory=list)
    #: YOLO's classes by their numbers.
    names: list[str] = field(default_factory=list)
    #: The field of a vector file holding the class.
    class_field: str = ""
    #: Annotation class → kind of the classification.
    class_map: dict[str, str] = field(default_factory=dict)
    added: str = ""

    def to_json(self) -> dict:
        out: dict = {"format": self.format}
        if self.names:
            out["names"] = list(self.names)
        if self.class_field:
            out["class_field"] = self.class_field
        if self.class_map:
            out["class_map"] = dict(self.class_map)
        if self.added:
            out["added"] = self.added
        out["items"] = [i.to_json() for i in self.items]
        return out

    def kind_of(self, name: str) -> str:
        return self.class_map.get(name, name)


@dataclass
class Dataset:
    name: str = ""
    groups: list[Group] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    #: What was counted when it was written: images, objects, classes (annotation names → count).
    stats: dict = field(default_factory=dict)

    @property
    def items(self) -> list[tuple[Group, Item]]:
        return [(g, i) for g in self.groups for i in g.items]

    def images(self) -> set[str]:
        return {os.path.normcase(os.path.abspath(i.image)) for g in self.groups for i in g.items}

    def to_json(self) -> dict:
        return {"format": FORMAT, "version": VERSION, "name": self.name, "created": self.created, "updated": self.updated,
                "stats": self.stats, "groups": [g.to_json() for g in self.groups]}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_dataset(path: str) -> Dataset:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise DatasetError(f"データセットを読めず: {os.path.basename(path)}（{e}）")
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise DatasetError(f"教師データセットのファイルではない: {os.path.basename(path)}")
    if int(data.get("version", 1)) > VERSION:
        raise DatasetError(f"新しい版のデータセット（版 {data.get('version')}）: プラグインを更新")
    groups = []
    for g in data.get("groups") or []:
        fmt = str(g.get("format", ""))
        if fmt not in FORMATS:
            raise DatasetError(f"知らない注釈の形式「{fmt}」: {os.path.basename(path)}")
        items = [Item(str(i["image"]), str(i.get("labels", "")), str(i.get("key", ""))) for i in g.get("items") or [] if i.get("image")]
        groups.append(Group(fmt, items, [str(n) for n in g.get("names") or []], str(g.get("class_field", "")),
                            {str(k): str(v) for k, v in (g.get("class_map") or {}).items()}, str(g.get("added", ""))))
    return Dataset(str(data.get("name", "")), groups, str(data.get("created", "")), str(data.get("updated", "")), dict(data.get("stats") or {}))


def save_dataset(dataset: Dataset, path: str) -> None:
    dataset.updated = now()
    dataset.created = dataset.created or dataset.updated
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dataset.to_json(), f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# Finding the files.

def files_in(folder: str, extensions: Sequence[str], recursive: bool = True) -> list[str]:
    """The files of `folder` with one of `extensions` (absolute, sorted)."""
    out = []
    folder = os.path.abspath(folder)
    for here, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        out += [os.path.join(here, n) for n in names if os.path.splitext(n)[1].lower() in extensions and not n.startswith(".")]
        if not recursive:
            break
    return sorted(out)


def _keys(path: str, root: str) -> tuple[str, str]:
    """A file's path under its root without its extension, and its name without it (both lower case)."""
    rel = os.path.splitext(os.path.relpath(path, root))[0].replace("\\", "/").lower()
    # YOLO's folders: images/… beside labels/….
    rel = re.sub(r"(^|/)(images|labels|annotations|imgs|anns)(/|$)", r"\1\3", rel).strip("/")
    return rel, os.path.basename(rel)


def pair(images: Sequence[str], image_root: str, labels: Sequence[str], label_root: str) -> tuple[list[Item], list[str]]:
    """
    Images paired with the annotation files of the same name: the same path
    under the two folders first (`images/train/a.tif` ↔ `labels/train/a.txt`),
    else the same file name when only one has it. Gives the pairs and the
    images without annotations.
    """
    by_path: dict[str, str] = {}
    by_name: dict[str, list[str]] = {}
    for p in labels:
        rel, name = _keys(p, label_root)
        by_path.setdefault(rel, p)
        by_name.setdefault(name, []).append(p)
    items, alone = [], []
    for image in images:
        rel, name = _keys(image, image_root)
        found = by_path.get(rel) or (by_name[name][0] if len(by_name.get(name, ())) == 1 else None)
        if found:
            items.append(Item(image, found))
        else:
            alone.append(image)
    return items, alone


def guess_format(path: str) -> Optional[str]:
    """The annotation format of a file, from its extension and first lines."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xml":
        return "voc"
    if ext == ".txt":
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = [line.split() for line in f.read().splitlines()[:20]]
        except OSError:
            return None
        rows = [r for r in lines if r and not r[0].startswith(("imagesource", "gsd"))]
        if not rows:
            return None
        if all(len(r) in (9, 10) and not _is_number(r[8]) for r in rows):
            return "dota"
        return "yolo"
    if ext == ".json":
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(4096)
        except OSError:
            return None
        if '"annotations"' in head or '"images"' in head or '"categories"' in head:
            return "coco"
        return "vector"
    if ext in VECTOR_EXTENSIONS:
        return "vector"
    return None


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def yolo_names(label_root: str, image_root: str = "") -> list[str]:
    """YOLO's class names from classes.txt / obj.names / data.yaml beside or above the annotations."""
    seen = set()
    for root in (label_root, image_root):
        here = os.path.abspath(root) if root else ""
        for _ in range(3):
            if not here or here in seen:
                break
            seen.add(here)
            for name in YOLO_NAME_FILES:
                p = os.path.join(here, name)
                if os.path.isfile(p):
                    names = names_from_yaml(p) if name.endswith((".yaml", ".yml")) else names_from_text(p)
                    if names:
                        return names
            parent = os.path.dirname(here)
            here = parent if parent != here else ""
    return []


def names_from_text(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return [line.strip() for line in f.read().splitlines() if line.strip()]


def names_from_yaml(path: str) -> list[str]:
    """`names:` of a YOLO data.yaml: `[a, b]`, a `- a` list or a `0: a` map (no YAML library needed)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^names\s*:\s*(.*)$", line)
        if not m:
            continue
        rest = m.group(1).split(" #")[0].strip()
        if rest.startswith("["):
            return [_unquote(p) for p in rest.strip("[]").split(",") if p.strip()]
        out: dict[int, str] = {}
        for line2 in lines[i + 1:]:
            if line2.strip() and not line2[:1].isspace():
                break
            item = re.match(r"^\s+-\s*(.+)$", line2)
            keyed = re.match(r"^\s+(\d+)\s*:\s*(.+)$", line2)
            if item:
                out[len(out)] = _unquote(item.group(1))
            elif keyed:
                out[int(keyed.group(1))] = _unquote(keyed.group(2))
        if out:
            return [out.get(k, str(k)) for k in range(max(out) + 1)]
    return []


def _unquote(text: str) -> str:
    return text.split(" #")[0].strip().strip("'\"")


# Reading the annotations.

@dataclass
class PixelBox:
    """An annotated object in the image's pixels (x right, y down), or in fractions of the image when `fraction`."""
    name: str
    x1: float
    y1: float
    x2: float
    y2: float
    fraction: bool = False

    def in_pixels(self, width: int, height: int) -> "PixelBox":
        if not self.fraction:
            return self
        return PixelBox(self.name, self.x1 * width, self.y1 * height, self.x2 * width, self.y2 * height)


def read_yolo(text: str, names: Sequence[str]) -> list[PixelBox]:
    """YOLO rows `class cx cy w h` (fractions), or `class x1 y1 … x4 y4` (OBB), as boxes in fractions."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) < 5:
            continue
        try:
            number = int(float(p[0]))
            values = [float(v) for v in p[1:]]
        except ValueError:
            continue
        name = names[number] if 0 <= number < len(names) else str(number)
        if len(values) >= 8:
            xs, ys = values[0:8:2], values[1:8:2]
            out.append(PixelBox(name, min(xs), min(ys), max(xs), max(ys), True))
        else:
            cx, cy, w, h = values[:4]
            out.append(PixelBox(name, cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, True))
    return out


def read_dota(text: str) -> list[PixelBox]:
    """DOTA rows `x1 y1 x2 y2 x3 y3 x4 y4 class difficult` (pixels), as the boxes around them."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) < 9:
            continue
        try:
            values = [float(v) for v in p[:8]]
        except ValueError:
            continue
        xs, ys = values[0::2], values[1::2]
        out.append(PixelBox(p[8], min(xs), min(ys), max(xs), max(ys)))
    return out


def read_voc(text: str) -> list[PixelBox]:
    """Pascal VOC's objects (`bndbox`, or `robndbox` of rotated boxes) in pixels."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise DatasetError(f"XML を読めず（{e}）")
    out = []
    for o in root.iter("object"):
        name = (o.findtext("name") or "").strip()
        b = o.find("bndbox")
        try:
            if b is not None:
                x1, y1, x2, y2 = (float(b.findtext(k) or "nan") for k in ("xmin", "ymin", "xmax", "ymax"))
            else:
                r = o.find("robndbox")
                if r is None:
                    continue
                cx, cy, w, h = (float(r.findtext(k) or "nan") for k in ("cx", "cy", "w", "h"))
                a = float(r.findtext("angle") or 0)
                hw = (abs(w * math.cos(a)) + abs(h * math.sin(a))) / 2
                hh = (abs(w * math.sin(a)) + abs(h * math.cos(a))) / 2
                x1, y1, x2, y2 = cx - hw, cy - hh, cx + hw, cy + hh
        except ValueError:
            continue
        if name and all(v == v for v in (x1, y1, x2, y2)):
            # VOC counts pixels from 1.
            out.append(PixelBox(name, x1 - 1, y1 - 1, x2, y2))
    return out


@dataclass
class Coco:
    """A COCO file: the objects of each image, by the image's file name (and by its name without folders)."""
    boxes: dict[str, list[PixelBox]]

    def of(self, key: str) -> list[PixelBox]:
        return self.boxes.get(key) or self.boxes.get(os.path.basename(key).lower(), [])


def read_coco(text: str) -> Coco:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise DatasetError(f"COCO の JSON を読めず（{e}）")
    names = {c["id"]: str(c.get("name", c["id"])) for c in data.get("categories") or []}
    files = {im["id"]: str(im.get("file_name", "")) for im in data.get("images") or []}
    by_id: dict = {i: [] for i in files}
    for a in data.get("annotations") or []:
        bbox = a.get("bbox")
        if a.get("image_id") not in by_id or not bbox or len(bbox) < 4:
            continue
        x, y, w, h = (float(v) for v in bbox[:4])
        by_id[a["image_id"]].append(PixelBox(names.get(a.get("category_id"), str(a.get("category_id"))), x, y, x + w, y + h))
    out: dict[str, list[PixelBox]] = {}
    for i, name in files.items():
        out[name] = by_id[i]
        out.setdefault(os.path.basename(name).lower(), by_id[i])
    return Coco(out)


def coco_files(coco_path: str) -> list[str]:
    """The images a COCO file names, as written."""
    with open(coco_path, encoding="utf-8") as f:
        data = json.load(f)
    return [str(im.get("file_name", "")) for im in data.get("images") or [] if im.get("file_name")]


class Reader:
    """Reads the annotations of a dataset's images (in pixels), keeping each COCO file read once."""

    def __init__(self):
        self._coco: dict[str, Coco] = {}

    def boxes(self, group: Group, item: Item) -> list[PixelBox]:
        """The objects of an image (fractions for YOLO); an image without an annotation file has none (background)."""
        if group.format == "vector":
            raise DatasetError("ベクタの注釈は QGIS で読む")
        if not item.labels:
            return []
        try:
            if group.format == "coco":
                coco = self._coco.get(item.labels)
                if coco is None:
                    with open(item.labels, encoding="utf-8") as f:
                        coco = self._coco[item.labels] = read_coco(f.read())
                return coco.of(item.key or os.path.basename(item.image))
            with open(item.labels, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            raise DatasetError(f"注釈を読めず: {item.labels}（{e.strerror or e}）")
        if group.format == "yolo":
            return read_yolo(text, group.names)
        if group.format == "dota":
            return read_dota(text)
        return read_voc(text)


def count_classes(boxes: Iterable[PixelBox], into: dict[str, int]) -> dict[str, int]:
    for b in boxes:
        into[b.name] = into.get(b.name, 0) + 1
    return into


def parse_class_map(text: str) -> dict[str, str]:
    """`car=車両, ship=船舶 > 支援船` (also `:` and `→`) as a map."""
    out = {}
    for part in re.split(r"[,、，;\n]+", text or ""):
        m = re.match(r"^\s*(.+?)\s*(?:=|:|→|->)\s*(.+?)\s*$", part)
        if m:
            out[m.group(1)] = m.group(2)
        elif part.strip():
            raise DatasetError(f"クラスの対応は「名前=種別」で指定: {part.strip()}")
    return out


# Making a group.

@dataclass
class Found:
    group: Group
    #: Images without annotations (left out unless kept as background).
    alone: list[str] = field(default_factory=list)
    #: Annotation files without an image.
    unused: int = 0


def make_group(image_folder: str, labels: str, fmt: str = "", names: Sequence[str] = (), class_field: str = "",
               class_map: Optional[dict] = None, recursive: bool = True, background: bool = False) -> Found:
    """
    The images of `image_folder` paired with their annotations: `labels`
    is a folder of one file per image (same names), or one file for all (a
    COCO JSON, a vector file covering the images). `fmt` empty: guessed from
    the files. With `background`, images without annotations are kept as
    images with nothing on them.
    """
    if not os.path.isdir(image_folder):
        raise DatasetError(f"画像のフォルダーがない: {image_folder}")
    images = files_in(image_folder, IMAGE_EXTENSIONS, recursive)
    if not images:
        raise DatasetError(f"画像なし（{', '.join(IMAGE_EXTENSIONS[:5])} など）: {image_folder}")
    labels = os.path.abspath(labels) if labels else os.path.abspath(image_folder)
    if os.path.isfile(labels):
        fmt = fmt or guess_format(labels) or ""
        if fmt == "coco":
            coco_names = coco_files(labels)
            items, alone = pair(images, image_folder, [os.path.join(os.path.dirname(labels), n) for n in coco_names], os.path.dirname(labels))
            keys = {os.path.normcase(os.path.join(os.path.dirname(labels), n)): n for n in coco_names}
            items = [Item(i.image, labels, keys.get(os.path.normcase(i.labels), os.path.basename(i.image))) for i in items]
            unused = len(coco_names) - len(items)
        elif fmt == "vector":
            items, alone, unused = [Item(i, labels) for i in images], [], 0
        else:
            raise DatasetError("注釈が 1 ファイルなら COCO の JSON かベクタ（GeoJSON・GeoPackage など）。画像ごとの注釈ならフォルダーを指定")
    elif os.path.isdir(labels):
        exts = {"yolo": (".txt",), "dota": (".txt",), "voc": (".xml",), "vector": VECTOR_EXTENSIONS}.get(fmt, (".txt", ".xml") + VECTOR_EXTENSIONS)
        files = [f for f in files_in(labels, exts, recursive) if os.path.basename(f).lower() not in YOLO_NAME_FILES]
        if not fmt:
            guesses = [g for g in (guess_format(f) for f in files[:50]) if g]
            fmt = max(set(guesses), key=guesses.count) if guesses else ""
            if fmt == "coco":
                raise DatasetError("COCO の注釈は JSON ファイルを指定")
        if not fmt:
            raise DatasetError(f"注釈ファイルなし（YOLO .txt・DOTA .txt・VOC .xml・ベクタ）: {labels}")
        files = [f for f in files if guess_format(f) in (fmt, None) or fmt in ("yolo", "dota") and f.lower().endswith(".txt")]
        # Images are not annotations, though both may be .json / .tif beside each other.
        files = [f for f in files if os.path.splitext(f)[1].lower() not in IMAGE_EXTENSIONS]
        items, alone = pair(images, image_folder, files, labels)
        unused = len(files) - len({i.labels for i in items})
    else:
        raise DatasetError(f"注釈のフォルダー・ファイルがない: {labels}")
    if fmt not in FORMATS:
        raise DatasetError(f"知らない注釈の形式「{fmt}」")
    if background:
        items += [Item(i) for i in alone]
        items.sort(key=lambda i: i.image)
    names = list(names)
    if fmt == "yolo" and not names:
        names = yolo_names(labels if os.path.isdir(labels) else os.path.dirname(labels), image_folder)
    group = Group(fmt, items, names if fmt == "yolo" else [], class_field if fmt == "vector" else "", dict(class_map or {}), now())
    return Found(group, [] if background else alone, unused)


def add_group(dataset: Dataset, group: Group) -> int:
    """Adds a group, leaving out images the dataset has already; gives how many were left out."""
    have = dataset.images()
    kept = [i for i in group.items if os.path.normcase(os.path.abspath(i.image)) not in have]
    left = len(group.items) - len(kept)
    if kept:
        group.items = kept
        dataset.groups.append(group)
    return left


def missing_files(dataset: Dataset) -> list[str]:
    """Files a dataset names that are not there (moved, a drive not connected)."""
    out = []
    for g, i in dataset.items:
        for p in (i.image, i.labels):
            if p and not os.path.exists(p) and p not in out:
                out.append(p)
    return out
