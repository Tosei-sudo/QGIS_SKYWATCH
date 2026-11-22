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
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Optional, Sequence

from .settings import MASTERS, resolve_config

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
            table, token = mine.get("taxonomyTable"), (store or {}).get("token")
            if isinstance(token, str) and token:
                # The store's token, which the production's table was read with.
                if isinstance(table, str):
                    mine["taxonomyTable"] = {"url": table, "token": token}
                elif isinstance(table, dict) and "token" not in table:
                    table["token"] = token
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
    return value, taken


def made_whole(value: Any, source: str) -> Any:
    """A copy of the main settings file's value with its relative paths (./ ../) made whole (read from `source`)."""
    return _whole(copy.deepcopy(value), source)


def exercise_images(production: Any) -> bool:
    """Whether the production has 判読の練習's organisation's exercises (training.exercises)."""
    training = production.get("training") if isinstance(production, dict) else None
    return isinstance(training, dict) and bool(training.get("exercises"))


def without_tokens(value: Any) -> Any:
    """A settings file's value without its tokens (what is written to disk: the production's tokens are kept in memory only)."""
    if isinstance(value, list):
        return [without_tokens(v) for v in value]
    if isinstance(value, dict):
        return {k: without_tokens(v) for k, v in value.items() if k != "token"}
    return value


def config_text(value: dict, head: str = "") -> str:
    """A settings file's text of `value` (JSON, `head` as // comments above it)."""
    lines = [f"// {line}" for line in head.splitlines()]
    return "\n".join([*lines, json.dumps(value, ensure_ascii=False, indent=2)]) + "\n"


def names_text(names: Sequence[str]) -> str:
    """「衛星・分類体系」: the masters taken, in Japanese."""
    return "・".join(MASTERS[n] for n in names if n in MASTERS)
