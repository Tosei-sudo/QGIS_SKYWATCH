"""
Training labels from observations and the classification (分類体系): which
class a record's kind (種別) trains, how big its object is, and the order of
the classes.

A class is a kind cut to a depth of the tree (`船舶 > 支援船 > 〇〇級` at
depth 1 is `船舶`), so what the model finds is a kind the observation book
knows. Sizes come from the record (its length, measured by two points, and
heading), else from the class' specifications (全長・全幅), else a default.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ...obs.taxonomy import Taxon, class_by_name, find, kind_of, path_of, specs_at, within

#: Metres of a degree of latitude.
METRES_PER_DEGREE = 111_320.0


def kind_path(value: str, taxonomy: Sequence[Taxon]) -> list[str]:
    """
    The path of a kind written in a record or a layer: as it is when the
    classification has it (or it has several levels), else the class whose
    name or other name (別名・英名) it is (`〇〇級` → `船舶 > 支援船 > 〇〇級`).
    """
    path = path_of(value)
    if len(path) == 1 and taxonomy and not find(list(taxonomy), path):
        found = class_by_name(list(taxonomy), path[0])
        if found:
            return list(found)
    return path


def class_of(value: str, taxonomy: Sequence[Taxon], depth: int = 0, within_kinds: Sequence[str] = ()) -> Optional[str]:
    """
    The class a kind trains: its path cut to `depth` levels (0: all), None
    when it is empty or outside the kinds asked for (`within_kinds`, each a
    kind and everything below it).
    """
    path = kind_path(value, taxonomy)
    if not path:
        return None
    kind = kind_of(path)
    if within_kinds and not any(within(kind, kind_path(k, taxonomy)) for k in within_kinds):
        return None
    return kind_of(path[:depth] if depth > 0 else path)


def class_order(found: Iterable[str], taxonomy: Sequence[Taxon], previous: Sequence[str] = ()) -> list[str]:
    """
    The classes of a model: those it had (in their order, so it can go on
    training), then the new ones in the classification's order, then those
    the classification does not have, by name.
    """
    order = {kind_of(t.path): i for i, t in enumerate(taxonomy)}
    out = list(previous)
    have = set(out)
    new = sorted({c for c in found if c not in have}, key=lambda c: (c not in order, order.get(c, 0), c))
    return out + new


@dataclass
class ObjectSize:
    """An object's footprint: length along its heading and width across, in metres."""
    length: float
    width: float
    #: Degrees from north, clockwise; None when unknown (the box is then the square around any heading).
    heading: Optional[float]
    #: Where the size came from: 記録, 諸元 or 既定.
    source: str


def _number(text: Optional[str]) -> Optional[float]:
    try:
        v = float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 and math.isfinite(v) else None


def object_size(kind_path_: Sequence[str], taxonomy: Sequence[Taxon], length: Optional[float] = None, heading: Optional[float] = None,
                default: float = 20.0) -> ObjectSize:
    """
    How big a recorded object is: the record's length (and heading), the
    width from the class' 全幅 / 全長 ratio; else the class' 全長 and 全幅;
    else a `default` square.
    """
    specs = specs_at(list(taxonomy), list(kind_path_)) if kind_path_ else {}
    spec_length = _number(specs.get("全長", ("",))[0])
    spec_width = _number(specs.get("全幅", ("",))[0])
    if heading is not None and not math.isfinite(heading):
        heading = None
    if length and length > 0:
        width = length * spec_width / spec_length if spec_length and spec_width else length
        return ObjectSize(length, width, heading, "記録")
    if spec_length:
        return ObjectSize(spec_length, spec_width or spec_length, heading, "諸元")
    return ObjectSize(default, default, None, "既定")


def footprint(lon: float, lat: float, size: ObjectSize) -> list[tuple[float, float]]:
    """
    The object's corners (longitude, latitude) around its centre: its
    rectangle turned to its heading, or, the heading unknown, the square
    holding it at any heading.
    """
    if size.heading is None:
        half = max(size.length, size.width) / 2
        corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
    else:
        a = math.radians(size.heading)
        # Along the heading (north = +y, clockwise) and across it.
        ux, uy = math.sin(a), math.cos(a)
        vx, vy = uy, -ux
        hl, hw = size.length / 2, size.width / 2
        corners = [(ux * sl * hl + vx * sw * hw, uy * sl * hl + vy * sw * hw) for sl, sw in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    k = max(math.cos(math.radians(lat)), 1e-6)
    return [(lon + x / (METRES_PER_DEGREE * k), lat + y / METRES_PER_DEGREE) for x, y in corners]
