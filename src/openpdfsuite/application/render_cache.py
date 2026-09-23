"""LRU page-image cache with a documented memory budget (AGENTS.md §13).

Keys include document id, revision, page, zoom (bucketed to 4 decimals) and
DPR — any of those changing invalidates entries naturally. Budget default:
256 MB of uncompressed pixmap bytes (w*h*4).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass

DEFAULT_BUDGET_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class CacheKey:
    doc_id: str
    revision: int
    page: int
    zoom: float
    dpr: float

    def __post_init__(self):
        object.__setattr__(self, "zoom", round(self.zoom, 4))


class RenderCache:
    def __init__(self, budget_bytes: int = DEFAULT_BUDGET_BYTES):
        self.budget = budget_bytes
        self._items: OrderedDict[Hashable, tuple[object, int]] = OrderedDict()
        self._used = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: CacheKey):
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        self._items.move_to_end(key)
        return entry[0]

    def put(self, key: CacheKey, pixmap) -> None:
        size = pixmap.width() * pixmap.height() * 4
        if size > self.budget:  # single page larger than the whole budget: skip
            return
        if key in self._items:
            self._used -= self._items[key][1]
            del self._items[key]
        self._items[key] = (pixmap, size)
        self._used += size
        self._items.move_to_end(key)
        self._evict()

    def _evict(self) -> None:
        while self._used > self.budget and self._items:
            _key, (_pix, size) = self._items.popitem(last=False)
            self._used -= size

    def invalidate_doc(self, doc_id: str) -> None:
        for key in [k for k in self._items if getattr(k, "doc_id", None) == doc_id]:
            _pix, size = self._items.pop(key)
            self._used -= size

    def invalidate_revision(self, doc_id: str, revision: int) -> None:
        """Drop everything except the given (current) revision."""
        for key in [k for k in self._items
                    if getattr(k, "doc_id", None) == doc_id and k.revision != revision]:
            _pix, size = self._items.pop(key)
            self._used -= size

    def clear(self) -> None:
        self._items.clear()
        self._used = 0

    @property
    def used_bytes(self) -> int:
        return self._used

    def __len__(self) -> int:
        return len(self._items)
