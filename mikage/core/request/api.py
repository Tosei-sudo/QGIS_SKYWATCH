"""
撮像依頼's door (窓口) for the rest of the plugin (docs/architecture.md R2): what the other
features, the screens and the plugin import of it. Its modules import each other directly;
everything outside imports through here, so its inside can be rearranged behind it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..door import lazy

if TYPE_CHECKING:  # the door (core/door.py): each name is imported from its module when first taken
    from .adoption import (
        answer_changes, manual_changes, manual_unrequested, match_answers, NOT_IN_FILE, read_window, Reading, unrequested_rows,
    )
    from .bulk import apply_change, RequestChange
    from .chances import ahead_span, chances_of, FORM_DAYS, form_span, form_text, wanted
    from .covered import read_covered
    from .export import convert_priority, encoded, files_of, leftover_files, render, row_rings, write
    from .follow import follow_and_save, found_text, in_days, marked, missed_ids, next_step, save_marked, tally, with_settled_times
    from .follow_job import main, requests_of
    from .from_plan import requests_of_plan
    from .geometry import look_text, read_last_looks
    from .groups import (
        check_group, first_group, listed, matches, MEMBERS_MAX, members_text, MINE, mine, NAME_MAX as GROUP_NAME_MAX, NO_GROUP,
        NOTE_MAX as GROUP_NOTE_MAX, offered, order_changes, read_groups, RequestGroup, same, users_of,
    )
    from .last import remembered, with_remembered
    from .linking import unit_key
    from .model import (
        bbox_of, CATEGORIES, day_bounds, ImagingRequest, JST, jst_day, level_of, PRIORITY_LEVELS, PRIORITY_MAX, PRIORITY_MIN,
        priority_text, square_rings,
    )
    from .passes import day_passes, find_passes
    from .pick import checks_of, Picked, picked, summary, unknown
    from .place_plans import HEAD, LABEL, Lane, lane_at, Layout, layout, Mark, mark_tip, marks_at, REQUEST, ROW
    from .quota import QuotaData, read_quota_data
    from .repository import RequestRepository, ScheduleRepository
    from .rows import code_text, field_roles, NUMBERS, request_of_row, row_of, row_problems, WRITTEN
    from .shot_view import _time, companions, shot_text, shot_tip
    from .simulate import (
        change_of, changed_systems, deadline_text, horizon_days, MOST_DAYS, run_passes, simulate, SUMMARY, SystemChange,
    )
    from .store import (
        add_history, add_shots, ADOPTION_ROLES, AWAITING, count_history, day_counts, DECLINED, DEFAULT_REQUEST_FIELDS,
        delete_history_rows, delete_request, delete_shots, followed_where, handed_where, HELD_BACK, history_has, history_where,
        HistoryRow, IMAGE_ARRIVED, load_day, load_days, load_history, load_requests, load_shots, load_shots_where,
        load_system_rows, MANUAL, new_request_id, PLANNED, replaced_where, RequestStoreConfig, save_request, save_requests, Saved,
        shot_days_where, shots_replaced_where, state_of, STATES, UNREQUESTED, update_history, update_requests, update_shots,
        upload_requests, waiting_ids,
    )
    from .use_time import read_other_uses, UseBook
    from .view import BOOST, CandidateFilter, NO_SYSTEM, series_choices, shown
    from .workflow import (
        boost_changes, clouds_of, DayPlan, forecast_places, history_of, keep_answers, missed_done, new_run_id, not_written,
        old_tle_text, orbiting, plan_day, radar_of, Rewrite, shots_of, written_summary,
    )
    from .daily import (
        HELD, TAKEN, chance_text, cloud_tip, cover_text, time_text as daily_time_text, kind_text, merge_text,
        occurrence_text, pace_text, part_text, pass_text, room_text, satellite_name,
    )
    from .linking import (
        MAX_IMAGES, find_images, gap_text, link_findings, link_text, time_text as linking_time_text, units_of,
        unlink_findings, with_links,
    )
    from .model import (
        ACHIEVES, LOOK_SIDES, ORBIT_DIRS, KINDS as REQUEST_KINDS, Repeat, STATUSES, TIMES_OF_DAY, repeat_of, repeat_text,
        shape_text,
    )
    from .place_plans import (
        DAYS, DAY_CHOICES, MOST_PLACES, NOT_HELD_BACK, KINDS as PLACE_KINDS, Place, SHOWN, lanes_of, mark_name, next_of,
        sort_by_next, span_of, taken_text, tally_text,
    )
    from .quota import MISSED, NO_ROWS, PACE_AHEAD, PENDING, REJECTED as QUOTA_REJECTED, UNCOUNTED_REASONS, USED, Usage
    from .practice_answer import answer_bytes, handed_out
    from .result_file import ADOPTED, REJECTED as RESULT_REJECTED, ResultColumn, ResultFile, WAITING, read_answers

__getattr__, __dir__ = lazy(__name__)

__all__ = [
    "RequestRepository", "ScheduleRepository",
    "_time", "add_history", "add_shots", "ADOPTION_ROLES", "ahead_span", "answer_bytes", "answer_changes", "apply_change", "AWAITING",
    "bbox_of", "BOOST", "boost_changes", "CandidateFilter", "CATEGORIES", "chances_of", "change_of", "changed_systems",
    "check_group", "checks_of", "clouds_of", "code_text", "companions", "convert_priority", "count_history", "day_bounds", "day_counts",
    "day_passes", "DayPlan", "deadline_text", "DECLINED", "DEFAULT_REQUEST_FIELDS", "delete_history_rows", "delete_request",
    "delete_shots", "encoded", "field_roles", "files_of", "find_passes", "first_group", "follow_and_save", "followed_where",
    "forecast_places", "FORM_DAYS", "GROUP_NAME_MAX", "GROUP_NOTE_MAX", "form_span", "form_text", "found_text", "handed_out", "handed_where", "HEAD", "HELD_BACK", "history_has",
    "history_of", "history_where", "HistoryRow", "horizon_days", "IMAGE_ARRIVED", "ImagingRequest", "in_days", "JST",
    "jst_day", "keep_answers", "LABEL", "Lane", "lane_at", "Layout", "layout", "leftover_files", "level_of", "listed",
    "load_day", "load_days", "load_history", "load_requests", "load_shots", "load_shots_where", "load_system_rows",
    "look_text", "main", "manual_changes", "manual_unrequested", "Mark", "mark_tip", "marked", "marks_at", "match_answers",
    "MANUAL", "matches", "MEMBERS_MAX", "members_text", "MINE", "mine", "missed_done", "missed_ids", "MOST_DAYS", "new_request_id", "new_run_id", "next_step",
    "NO_GROUP", "NO_SYSTEM", "NOT_IN_FILE", "not_written", "NUMBERS", "offered", "old_tle_text", "orbiting", "order_changes", "Picked",
    "picked", "plan_day", "PLANNED", "PRIORITY_LEVELS", "PRIORITY_MAX", "PRIORITY_MIN", "priority_text", "QuotaData",
    "radar_of", "read_covered", "read_groups", "read_last_looks", "read_other_uses", "read_quota_data", "read_window",
    "Reading", "remembered", "render", "replaced_where", "REQUEST", "request_of_row", "RequestChange", "RequestGroup",
    "requests_of", "requests_of_plan", "RequestStoreConfig", "Rewrite", "ROW", "row_of", "row_problems", "row_rings",
    "run_passes", "same", "save_marked", "save_request", "save_requests", "Saved", "series_choices", "shot_days_where",
    "shot_text", "shot_tip", "shots_of", "shots_replaced_where", "shown", "simulate", "square_rings", "state_of", "STATES",
    "SUMMARY", "summary", "SystemChange", "tally", "unit_key", "unknown", "UNREQUESTED", "unrequested_rows", "update_history",
    "update_requests", "update_shots", "upload_requests", "UseBook", "users_of", "waiting_ids", "wanted", "with_remembered",
    "with_settled_times", "write", "WRITTEN", "written_summary",
    "ACHIEVES", "ADOPTED", "DAYS", "DAY_CHOICES", "HELD", "LOOK_SIDES", "MAX_IMAGES", "MISSED", "MOST_PLACES",
    "NOT_HELD_BACK", "NO_ROWS", "ORBIT_DIRS", "PACE_AHEAD", "PENDING", "PLACE_KINDS", "Place", "QUOTA_REJECTED",
    "REQUEST_KINDS", "RESULT_REJECTED", "Repeat", "ResultColumn", "ResultFile", "SHOWN", "STATUSES", "TAKEN", "TIMES_OF_DAY", "UNCOUNTED_REASONS",
    "USED", "Usage", "WAITING", "chance_text", "cloud_tip", "cover_text", "daily_time_text", "find_images", "gap_text",
    "kind_text", "lanes_of", "link_findings", "link_text", "linking_time_text", "mark_name", "merge_text", "next_of",
    "occurrence_text", "pace_text", "part_text", "pass_text", "read_answers", "repeat_of", "repeat_text", "room_text",
    "satellite_name", "shape_text", "sort_by_next", "span_of", "taken_text", "tally_text", "units_of",
    "unlink_findings", "with_links",
]
