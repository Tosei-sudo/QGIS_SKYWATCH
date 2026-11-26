"""
The settings file's `training` (練習モード):

* absent or `true`: 練習モード with the built-in sample (Tokyo Bay, Yokosuka);
* `false`: 練習モード hidden;
* `{ "config": "<path or URL>" }`: the organisation's own practice settings
  file (its own practice services, e.g. practice tables on SDE, real images)
  instead of the built-in sample; a relative path is the main settings
  file's folder's. `"enabled": false` hides it as `false` does.

With the built-in sample, the masters that are only read (satellites, the
taxonomy, places, the events' kinds, the notes' glossary: `MASTERS`) are the
main settings file's while practising (`"fromProduction"`, all of them by
default; `false` for none, or a list of their names): the practice is on the
organisation's own classes and satellites. What is written stays on the
practice server.

`"scoring"`: 判読の練習's scoring as the organisation wants it (each exercise
may change some of it; exercises.py's Scoring): `{"tolerance": 50,
"weights": {"find": 50, "kind": 30, "activity": 10, "measure": 10}, …}`.

`"exercises"`: 判読の練習's organisation's exercises, their answers and the
results' table (tables.py): `{"exercises": "<url>", "answers": "<url>",
"results": "<url>"}`. Read (and the results written) on the
production while practising.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from .exercises import DEFAULT_SCORING, Scoring, scoring_of
from .tables import exercise_tables_of, ExerciseTables

KEYS = ("enabled", "config", "fromProduction", "scoring", "exercises")
#: The masters read from the main settings file while practising (masters.py), with their names in Japanese.
MASTERS = {"satellites": "衛星", "taxonomy": "分類体系", "places": "地名", "eventKinds": "イベントの種類", "glossary": "備考の単語集"}


@dataclass(frozen=True)
class TrainingSettings:
    enabled: bool = True
    #: The organisation's practice settings file ("" : the built-in sample and server).
    config: str = ""
    #: The masters taken from the main settings file while practising on the built-in sample (MASTERS' keys).
    from_production: tuple[str, ...] = tuple(MASTERS)
    #: 判読の練習's scoring (the organisation's).
    scoring: Scoring = DEFAULT_SCORING
    #: 判読の練習's organisation's exercises (None: the sample's only).
    exercises: Optional[ExerciseTables] = None

    @property
    def builtin(self) -> bool:
        """Whether the built-in sample is practised on (the plugin's own server)."""
        return not self.config


DEFAULT_TRAINING = TrainingSettings()


def training_settings_of(value: Any, problems: list[str]) -> TrainingSettings:
    if isinstance(value, bool):
        return TrainingSettings(enabled=value)
    if not isinstance(value, dict):
        problems.append("training は true・false か { \"config\": \"設定ファイル\" } で指定")
        return DEFAULT_TRAINING
    unknown = [k for k in value if k not in KEYS]
    if unknown:
        problems.append(f"training に不明なキー「{'・'.join(unknown)}」（{'・'.join(KEYS)}）")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append("training.enabled は true か false で指定")
        enabled = True
    config = value.get("config", "")
    if not isinstance(config, str):
        problems.append("training.config は設定ファイルのパスか URL で指定")
        config = ""
    names = value.get("fromProduction", True)
    if names is True:
        names = list(MASTERS)
    elif names is False:
        names = []
    elif not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        problems.append(f"training.fromProduction は true・false か名前の配列（{'・'.join(MASTERS)}）で指定")
        names = list(MASTERS)
    unknown = [n for n in names if n not in MASTERS]
    if unknown:
        problems.append(f"training.fromProduction に不明な名前「{'・'.join(unknown)}」（{'・'.join(MASTERS)}）")
    scoring = scoring_of(value.get("scoring"), problems)
    return TrainingSettings(enabled=enabled, config=config.strip(), from_production=tuple(n for n in MASTERS if n in names),
                            scoring=scoring, exercises=exercise_tables_of(value.get("exercises"), problems))


def resolve_config(config: str, main_source: str) -> str:
    """`training.config` as a path or URL: a relative path is the main settings file's folder's (a URL's too)."""
    if not config or re.match(r"^[a-z][a-z0-9+.-]*://", config, re.IGNORECASE) or re.match(r"^([a-zA-Z]:)?[\\/]", config):
        return config
    if re.match(r"^https?://", main_source or "", re.IGNORECASE):
        return main_source.rsplit("/", 1)[0] + "/" + config.replace("\\", "/")
    folder = os.path.dirname(os.path.abspath(main_source)) if main_source else os.getcwd()
    return os.path.normpath(os.path.join(folder, config))
