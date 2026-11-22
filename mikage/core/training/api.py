"""
練習モード's door (窓口) for the rest of the plugin (docs/architecture.md R2): the
practice server, its sample, the settings file's `training`, チュートリアル's
progress, and 判読の練習's exercises, scoring and results.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..door import lazy

if TYPE_CHECKING:  # the door (core/door.py): each name is imported from its module when first taken
    from . import masters, sample
    from .exercises import (
        Answer, CAME, CHANGE, CHANGES, DEFAULT_SCORING, EXAM, Exercise, EXTRA, fitted, FIND, GONE, hint, IDENTIFY, ITEM_NAMES, ITEMS,
        kind_text, Mark, MEASURE, MISSED, MISSED_BONUS, Outcome, PRACTICE, RIGHT, score, Scoring, scoring_of, STAYED, TYPES,
    )
    from .progress import kept, read_progress, write_progress
    from .results import add_result, best_of, confusions, exam_taken, mistaken, read_results, result_record, tries_of
    from .sample import AREA, image_plans, training_config
    from .sample_exercises import sample_exercises
    from .server import open_data, rebase, Store, TrainingServer
    from .settings import DEFAULT_TRAINING, MASTERS, resolve_config, training_settings_of, TrainingSettings

__getattr__, __dir__ = lazy(__name__)

__all__ = [
    "add_result", "Answer", "AREA", "best_of", "CAME", "CHANGE", "CHANGES", "confusions", "DEFAULT_SCORING", "DEFAULT_TRAINING",
    "EXAM", "exam_taken", "Exercise", "EXTRA", "FIND", "fitted", "GONE", "hint", "IDENTIFY", "image_plans", "ITEM_NAMES", "ITEMS",
    "kept", "kind_text", "Mark", "masters", "MASTERS", "MEASURE", "MISSED", "MISSED_BONUS", "mistaken", "open_data", "Outcome",
    "PRACTICE", "read_progress", "read_results", "rebase", "resolve_config", "result_record", "RIGHT", "sample", "sample_exercises",
    "score", "Scoring", "scoring_of", "STAYED", "Store", "training_config", "training_settings_of", "TrainingServer",
    "TrainingSettings", "tries_of", "TYPES", "write_progress",
]
