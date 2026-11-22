"""
練習モード's door (窓口) for the rest of the plugin (docs/architecture.md R2): the
practice server, its sample and the settings file's `training`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..door import lazy

if TYPE_CHECKING:  # the door (core/door.py): each name is imported from its module when first taken
    from . import masters, sample
    from .sample import AREA, image_plans, training_config
    from .server import open_data, rebase, Store, TrainingServer
    from .settings import DEFAULT_TRAINING, MASTERS, resolve_config, training_settings_of, TrainingSettings

__getattr__, __dir__ = lazy(__name__)

__all__ = [
    "AREA", "DEFAULT_TRAINING", "image_plans", "masters", "MASTERS", "open_data", "rebase", "resolve_config", "sample", "Store", "training_config",
    "training_settings_of", "TrainingServer", "TrainingSettings",
]
