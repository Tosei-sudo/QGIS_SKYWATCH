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
    from .authoring import (
        answers_from_records, check, copied, delete_draft, Draft, DraftAnswer, drafts_of, exercise_of_draft, new_exercise_id, read_drafts,
        save_draft, set_active,
    )
    from .grades import (
        advice, Cell, Column, confusion, courses_of, Grades, grades, grades_csv, in_period, PERIODS, read_all_results, taken_for_others,
    )
    from .exercises import (
        Answer, CAME, CHANGE, CHANGES, DEFAULT_SCORING, EXAM, Exercise, EXTRA, fitted, FIND, GONE, hint, IDENTIFY, ITEM_NAMES, ITEMS,
        kind_text, Mark, MEASURE, metres, MISSED, MISSED_BONUS, Outcome, PRACTICE, RIGHT, score, Scoring, scoring_of, STAYED, TYPES,
    )
    from .progress import kept, read_progress, write_progress
    from .results import (
        add_result, best_of, confusions, exam_taken, mark_sent, merged_results, mistaken, read_results, result_record, tries_of, unsent,
    )
    from .sample import AREA, image_plans, training_config
    from .sample_exercises import sample_exercises
    from .server import open_data, rebase, Store, TrainingServer
    from .settings import DEFAULT_TRAINING, MASTERS, resolve_config, training_settings_of, TrainingSettings
    from .tables import (
        ANSWER_FIELDS, EXERCISE_FIELDS, ExerciseTables, org_exercises, read_exercises, read_results_of, RESULT_FIELDS, result_attributes,
        send_results,
    )

__getattr__, __dir__ = lazy(__name__)

__all__ = [
    "add_result", "advice", "Answer", "ANSWER_FIELDS", "answers_from_records", "AREA", "best_of", "CAME", "Cell", "CHANGE", "CHANGES", "check",
    "Column", "confusion", "confusions", "copied", "courses_of", "DEFAULT_SCORING", "DEFAULT_TRAINING", "delete_draft", "Draft", "DraftAnswer", "drafts_of",
    "EXAM", "exam_taken", "Exercise", "EXERCISE_FIELDS", "exercise_of_draft", "ExerciseTables", "EXTRA", "FIND", "fitted", "GONE", "Grades",
    "grades", "grades_csv", "hint", "IDENTIFY", "image_plans", "in_period", "ITEM_NAMES", "ITEMS", "kept", "kind_text", "Mark", "mark_sent",
    "masters", "MASTERS", "MEASURE", "merged_results", "metres", "MISSED", "MISSED_BONUS", "mistaken", "new_exercise_id", "open_data", "org_exercises",
    "Outcome", "PERIODS", "PRACTICE", "read_all_results", "read_drafts", "read_exercises", "read_progress", "read_results", "read_results_of",
    "rebase", "resolve_config", "RESULT_FIELDS", "result_attributes", "result_record", "RIGHT", "sample", "sample_exercises", "save_draft",
    "score", "Scoring", "scoring_of", "send_results", "set_active", "STAYED", "Store", "taken_for_others", "training_config",
    "training_settings_of", "TrainingServer", "TrainingSettings", "tries_of", "TYPES", "unsent", "write_progress",
]
