"""
The observation stores the settings name, as the viewer wires them
(main.ts): the Esri stores of `observationStores` first, then the local
GeoPackage, always there. Each store's kinds, activities, classification and
levels of 確度 are its own, else `observationVocabulary`'s; the
specification fields and the match radius are always the vocabulary's. Its
checked areas and classification go to its own layer and table when it
names them, else to the GeoPackage under its name.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Optional

from ..esri import EsriClient
from ..gpkg import GeoPackage
from .model import ObservationStoreConfig, ObservationVocabulary
from .stores import (
    LOCAL_LABEL, AreaStore, EsriAreaStore, EsriObservationStore, EsriTaxonomyStore, LocalAreaStore, LocalObservationStore,
    LocalTaxonomyStore, ObservationStore, TaxonomyStore,
)


@dataclass
class StoreSet:
    """A store of records with its areas, classification and vocabulary."""
    label: str
    records: ObservationStore
    areas: AreaStore
    taxonomy: TaxonomyStore
    vocabulary: ObservationVocabulary
    #: The settings' entry (None: the GeoPackage).
    config: Optional[ObservationStoreConfig] = None

    @property
    def remote(self) -> bool:
        """Whether it is asked over the network (so in a task)."""
        return self.config is not None


def store_vocabulary(config: Optional[ObservationStoreConfig], base: ObservationVocabulary) -> ObservationVocabulary:
    if config is None:
        return base
    return replace(
        base,
        kinds=list(config.kinds) if config.kinds else list(base.kinds),
        activities=list(config.activities) if config.activities else list(base.activities),
        confidences=list(config.confidences) if config.confidences else list(base.confidences),
        taxonomy=list(config.taxonomy) if config.taxonomy else list(base.taxonomy),
    )


def store_sets(configs: list[ObservationStoreConfig], vocabulary: ObservationVocabulary, gpkg: Callable[[], GeoPackage],
               client: Callable[[], EsriClient], token_for: Callable[[str, Optional[str]], Optional[str]]) -> list[StoreSet]:
    """Every store, the Esri ones first. `token_for(url, token)` gives the token to send (None where QGIS authenticates)."""
    out: list[StoreSet] = []
    for c in configs:
        esri = client()
        records = EsriObservationStore(c, esri, token_for(c.url, c.token))
        if c.areas:
            areas: AreaStore = EsriAreaStore(f"{c.label}の確認範囲", c.areas, esri, token_for(c.areas.url, c.areas.token))
        else:
            areas = LocalAreaStore(gpkg(), c.label)
        if c.taxonomy_table:
            taxonomy: TaxonomyStore = EsriTaxonomyStore(c.taxonomy_table, esri, f"{c.label}の分類テーブル",
                                                        token_for(c.taxonomy_table.url, c.taxonomy_table.token))
        else:
            taxonomy = LocalTaxonomyStore(gpkg(), c.label)
        out.append(StoreSet(c.label, records, areas, taxonomy, store_vocabulary(c, vocabulary), c))
    db = gpkg()
    out.append(StoreSet(LOCAL_LABEL, LocalObservationStore(db), LocalAreaStore(db, LOCAL_LABEL), LocalTaxonomyStore(db, LOCAL_LABEL), vocabulary))
    return out
