"""
練習モード on the built-in sample, with the masters that are only read taken
from the main (production) settings file: the organisation's own satellites,
classes, places, events' kinds and notes' glossary, while every table written
stays on the practice server (settings.py's `fromProduction`).

* satellites: satelliteCatalogs, sensorCatalogs, satelliteAliases and
  groundStations, and the imaging systems they are ordered from
  (requestExports, their files written into the practice folder instead);
* taxonomy: observationVocabulary, and the first observation store's classes
  (taxonomy, taxonomyTable, kinds, activities, confidences) on the practice
  store (its table is read only while practising: 分類体系の編集 is not usable);
* places: geocoders;
* eventKinds: events.kinds;
* glossary: glossary.

With 判読の練習's organisation's exercises (training.exercises), also the
production's pathMappings: the exercises' images may be the production
catalogs' (read only while practising).

A master the main settings file does not have stays the sample's. A path
written relative to the main settings file is made whole (the practice's
settings file is in another folder).

The image catalogs (tsudo 2026-11-23): the production's `imageCatalogs`
whose `mode` is "training" (練習モードだけ) or "both" (両方) are shown while
practising after the practice's own (with_practice_catalogs), on the built-in
sample and on the organisation's practice settings file alike, whatever
`fromProduction` says (the administrator chose them one by one); their
production's servers signed in to as in production (its `signIn`,
taken whenever anything is), their files found with the production's
pathMappings. MIKAGE only reads image catalogs.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Optional, Sequence

from .settings import MASTERS, resolve_config

#: The production's image catalogs shown while practising (imageCatalogs[].mode).
PRACTICE_MODES = ("training", "both")
SATELLITE_KEYS = ("satelliteCatalogs", "sensorCatalogs", "satelliteAliases", "groundStations", "requestExports")
#: An observation store's own classes (core/obs/model.py's observation_store_of).
STORE_VOCABULARY = ("taxonomy", "taxonomyTable", "kinds", "activities", "confidences")
_RELATIVE = re.compile(r"^\.{1,2}[\\/]")


def _whole(value: Any, source: str) -> Any:
    """`value` with the paths relative to the main settings file (./ or ../) made whole."""
    if isinstance(value, str):
        return resolve_config(value, source) if _RELATIVE.match(value) else value
    if isinstance(value, list):
        return [_whole(v, source) for v in value]
    if isinstance(value, dict):
        return {k: _whole(v, source) for k, v in value.items()}
    return value


def _first_store(config: dict) -> Optional[dict]:
    stores = config.get("observationStores")
    return stores[0] if isinstance(stores, list) and stores and isinstance(stores[0], dict) else None


def with_production(practice: dict, production: Any, names: Sequence[str], source: str, folder: str) -> tuple[dict, list[str]]:
    """
    The practice's settings (`practice`, a settings file's value) with the
    masters `names` of the main settings file's value `production` (read from
    `source`); the systems' files into `folder`. Also the names taken (those
    the main settings file has), in MASTERS' order.
    """
    value = copy.deepcopy(practice)
    if not isinstance(production, dict):
        return value, []
    prod = _whole(copy.deepcopy(production), source)
    taken: list[str] = []
    for name in MASTERS:
        if name not in names:
            continue
        if name == "satellites" and any(k in prod for k in ("satelliteCatalogs", "sensorCatalogs")):
            for key in SATELLITE_KEYS:
                if key in prod:
                    value[key] = prod[key]
                else:
                    value.pop(key, None)
            systems = value.get("requestExports")
            for system in systems if isinstance(systems, list) else []:
                if isinstance(system, dict):
                    system["folder"] = folder
            taken.append(name)
        elif name == "taxonomy":
            store, mine = _first_store(prod), _first_store(value)
            has = "observationVocabulary" in prod or (store is not None and any(k in store for k in STORE_VOCABULARY))
            if not has or mine is None:
                continue
            if "observationVocabulary" in prod:
                value["observationVocabulary"] = prod["observationVocabulary"]
            else:
                value.pop("observationVocabulary", None)
            for key in STORE_VOCABULARY:
                if store is not None and key in store:
                    mine[key] = store[key]
                else:
                    mine.pop(key, None)
            taken.append(name)
        elif name == "places" and "geocoders" in prod:
            value["geocoders"] = prod["geocoders"]
            taken.append(name)
        elif name == "eventKinds" and isinstance(prod.get("events"), dict) and isinstance(value.get("events"), dict):
            if "kinds" in prod["events"]:
                value["events"]["kinds"] = prod["events"]["kinds"]
            else:
                value["events"].pop("kinds", None)
            taken.append(name)
        elif name == "glossary" and "glossary" in prod:
            value["glossary"] = prod["glossary"]
            taken.append(name)
    if exercise_images(prod) and isinstance(prod.get("pathMappings"), list):
        # 判読の練習's organisation's exercises: their images may be the production catalogs' (read only),
        # their files where the production finds them.
        value["pathMappings"] = [*(value.get("pathMappings") or []), *prod["pathMappings"]]
    if (taken or exercise_images(prod)) and "signIn" in prod and "signIn" not in value:
        # The production's servers read while practising are signed in to as in production.
        value["signIn"] = prod["signIn"]
    return value, taken


def made_whole(value: Any, source: str) -> Any:
    """A copy of the main settings file's value with its relative paths (./ ../) made whole (read from `source`)."""
    return _whole(copy.deepcopy(value), source)


def exercise_images(production: Any) -> bool:
    """Whether the production has 判読の練習's organisation's exercises (training.exercises)."""
    training = production.get("training") if isinstance(production, dict) else None
    return isinstance(training, dict) and bool(training.get("exercises"))


def practice_catalog_entries(production: Any) -> list[dict]:
    """The production's `imageCatalogs` entries shown while practising (`mode` training or both), in its order."""
    entries = production.get("imageCatalogs") if isinstance(production, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("mode") in PRACTICE_MODES]


def with_practice_catalogs(config: Any, production: Any, source: str) -> list[str]:
    """
    The practice's settings as read (`config`, a PluginConfig, changed in
    place): every catalog of its own shown (a practice's settings file's
    `mode` is not looked at), then the production's shown while practising
    (practice_catalog_entries of the main settings file's value `production`,
    read from `source`; one with the same URL and conditions as one of the
    practice's left out) with the production's pathMappings after the
    practice's (where their files are). The labels of the production's taken.
    """
    from ..catalog.api import catalog_of, path_mappings_of

    own = [*config.image_catalogs, *config.practice_catalogs]
    config.image_catalogs, config.practice_catalogs = own, []
    entries = practice_catalog_entries(production)
    if not entries:
        return []
    whole = _whole(copy.deepcopy({"imageCatalogs": entries, "pathMappings": production.get("pathMappings")}), source)
    seen = {(c.url, c.where) for c in own}
    taken = []
    for i, entry in enumerate(whole["imageCatalogs"]):
        catalog = catalog_of(entry, [], i)  # (its problems are told in production)
        if catalog is None or (catalog.url, catalog.where) in seen:
            continue
        seen.add((catalog.url, catalog.where))
        taken.append(catalog)
    if not taken:
        return []
    config.image_catalogs = [*own, *taken]
    if isinstance(whole["pathMappings"], list):
        extra = [m for m in path_mappings_of(whole["pathMappings"], []) if m not in config.path_mappings]
        config.path_mappings = [*config.path_mappings, *extra]
    _with_sign_in(config, production)
    return [c.label for c in taken]


def _with_sign_in(config: Any, production: dict) -> None:
    """The production's `signIn` entries added to the practice's (a PluginConfig) for the servers it says nothing of."""
    from ..sign_in import sign_in_rules_of

    if production.get("signIn") is None:
        return
    have = {d for r in config.sign_in for d in r.domains}
    config.sign_in = [*config.sign_in, *(r for r in sign_in_rules_of(production["signIn"], []) if not set(r.domains) & have)]


def read_text(taken: Sequence[str], production: Any) -> str:
    """「衛星・分類体系・画像カタログ 2 件」: what is read from the production while practising (the masters `taken`, its catalogs)."""
    names = names_text(taken)
    count = len(practice_catalog_entries(production))
    catalogs = f"画像カタログ {count} 件" if count else ""
    return "・".join(t for t in (names, catalogs) if t)


def config_text(value: dict, head: str = "") -> str:
    """A settings file's text of `value` (JSON, `head` as // comments above it)."""
    lines = [f"// {line}" for line in head.splitlines()]
    return "\n".join([*lines, json.dumps(value, ensure_ascii=False, indent=2)]) + "\n"


def names_text(names: Sequence[str]) -> str:
    """「衛星・分類体系」: the masters taken, in Japanese."""
    return "・".join(MASTERS[n] for n in names if n in MASTERS)
