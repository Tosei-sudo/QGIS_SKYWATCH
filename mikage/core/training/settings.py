"""
The settings file's `training` (練習モード):

* absent or `true`: 練習モード with the built-in sample (Tokyo Bay, Yokosuka);
* `false`: 練習モード hidden;
* `{ "config": "<path or URL>" }`: the organisation's own practice settings
  file (its own practice services, e.g. practice tables on SDE, real images)
  instead of the built-in sample; a relative path is the main settings
  file's folder's. `"enabled": false` hides it as `false` does.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

KEYS = ("enabled", "config")


@dataclass(frozen=True)
class TrainingSettings:
    enabled: bool = True
    #: The organisation's practice settings file ("" : the built-in sample and server).
    config: str = ""

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
    return TrainingSettings(enabled=enabled, config=config.strip())


def resolve_config(config: str, main_source: str) -> str:
    """`training.config` as a path or URL: a relative path is the main settings file's folder's (a URL's too)."""
    if not config or re.match(r"^[a-z][a-z0-9+.-]*://", config, re.IGNORECASE) or re.match(r"^([a-zA-Z]:)?[\\/]", config):
        return config
    if re.match(r"^https?://", main_source or "", re.IGNORECASE):
        return main_source.rsplit("/", 1)[0] + "/" + config.replace("\\", "/")
    folder = os.path.dirname(os.path.abspath(main_source)) if main_source else os.getcwd()
    return os.path.normpath(os.path.join(folder, config))
