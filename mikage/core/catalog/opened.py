"""
開いた画像の履歴 (tsudo, 2026-11-18: 画像カタログから開くのを、もっともっと簡単に。
過去開いたパスと画像 ID を覚えておいて、確認の上開く).

Every image opened from a catalog (the catalog dock, the 新着画像 cards, the
events, the plans) is kept on this computer: its image id, the catalog, where
its files were found and which of them were opened, and the catalog's record
(so it opens again without asking the catalog, scanning the folder or
choosing the files). The newest first, one per image of a catalog, at most
`LIMIT`. Paths are this computer's (its drives and mapped folders), so the
list is never shared.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

#: How many images are kept (the oldest dropped).
LIMIT = 300
#: How many files of a folder are kept for the choice (those opened always are).
FILES_LIMIT = 200


@dataclass
class OpenedImage:
    image_id: str
    catalog_url: str
    catalog_label: str
    #: Where the images are: a file, a folder (`files` below it) or a URL.
    place: str
    is_url: bool
    #: The path as the catalog writes it.
    given: str
    #: The files found below a folder (paths with /, as `RecordImages.files`); [] for one file or a URL.
    files: list[str] = field(default_factory=list)
    #: Those of `files` opened last time.
    opened: list[str] = field(default_factory=list)
    #: The catalog's record (Esri JSON), for its layer's properties and its footprint.
    feature: dict[str, Any] = field(default_factory=dict)
    #: 撮像日時 (ms) when the catalog says.
    acquired: Optional[int] = None
    sensor: str = ""
    opened_at: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.catalog_url, self.image_id)

    def paths(self, files: Optional[list[str]] = None) -> list[str]:
        """The local paths of `files` (all of `files`, else the image itself); a URL as it is."""
        if not self.files:
            return [self.place]
        return [os.path.join(self.place, *rel.split("/")) for rel in (self.files if files is None else files)]

    def missing(self, files: Optional[list[str]] = None) -> list[str]:
        """Which of the files (the opened ones by default) are no longer there (never for a URL)."""
        if self.is_url:
            return []
        if not self.files:
            return [] if os.path.exists(self.place) else [os.path.basename(self.place.rstrip("/\\")) or self.place]
        chosen = self.opened if files is None else files
        return [rel for rel in chosen if not os.path.exists(os.path.join(self.place, *rel.split("/")))]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _entry_of(value: Any) -> Optional[OpenedImage]:
    if not isinstance(value, dict):
        return None
    try:
        e = OpenedImage(
            image_id=str(value["image_id"]), catalog_url=str(value.get("catalog_url") or ""), catalog_label=str(value.get("catalog_label") or ""),
            place=str(value["place"]), is_url=bool(value.get("is_url")), given=str(value.get("given") or ""),
            files=[str(f) for f in value.get("files") or [] if isinstance(f, str)],
            opened=[str(f) for f in value.get("opened") or [] if isinstance(f, str)],
            feature=value.get("feature") if isinstance(value.get("feature"), dict) else {},
            acquired=int(value["acquired"]) if isinstance(value.get("acquired"), (int, float)) else None,
            sensor=str(value.get("sensor") or ""), opened_at=int(value.get("opened_at") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return e if e.image_id and e.place else None


class OpenedImages:
    """The list, kept in a JSON file (read when first asked, written after each change)."""

    def __init__(self, path: str, limit: int = LIMIT):
        self.path = path
        self.limit = limit
        self._items: Optional[list[OpenedImage]] = None

    def items(self) -> list[OpenedImage]:
        """Newest first."""
        if self._items is None:
            self._items = []
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = []
            seen: set = set()
            for v in data if isinstance(data, list) else []:
                e = _entry_of(v)
                if e is not None and e.key not in seen:
                    seen.add(e.key)
                    self._items.append(e)
        return list(self._items)

    def _save(self) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        part = self.path + ".part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump([e.to_json() for e in self._items or []], f, ensure_ascii=False)
        os.replace(part, self.path)

    def add(self, entry: OpenedImage, now: Optional[float] = None) -> OpenedImage:
        """Keeps an image opened (first; replacing the same image of the same catalog)."""
        entry.opened_at = int(now if now is not None else time.time() * 1000)
        if len(entry.files) > FILES_LIMIT:
            entry.files = list(dict.fromkeys([*entry.opened, *entry.files]))[:max(FILES_LIMIT, len(entry.opened))]
        items = [e for e in self.items() if e.key != entry.key]
        self._items = [entry, *items][:self.limit]
        try:
            self._save()
        except OSError:
            pass  # kept for this session only
        return entry

    def find(self, image_id: str, catalog_url: Optional[str] = None) -> Optional[OpenedImage]:
        """The image opened last with that id (of that catalog when given)."""
        image_id = (image_id or "").strip()
        return next((e for e in self.items() if e.image_id == image_id and (catalog_url is None or e.catalog_url == catalog_url)), None)

    def remove(self, entry: OpenedImage) -> None:
        self._items = [e for e in self.items() if e.key != entry.key]
        try:
            self._save()
        except OSError:
            pass

    def recent(self, n: int = 20) -> list[OpenedImage]:
        return self.items()[:n]
