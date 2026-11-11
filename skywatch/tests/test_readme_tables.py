"""README 「Esri 側のテーブル定義」 against the code: every default attribute name, the naming rule, and the links."""
from __future__ import annotations

import os
import re
import unicodedata

from skywatch.core import edit_stamp, events
from skywatch.core.obs import alerts
from skywatch.core.obs.areas import DEFAULT_AREA_FIELDS
from skywatch.core.obs.assignments import DEFAULT_ASSIGNMENT_FIELDS
from skywatch.core.obs.handover import DEFAULT_HANDOVER_FIELDS
from skywatch.core.obs.model import DEFAULT_OBSERVATION_FIELDS
from skywatch.core.obs.taxonomy import DEFAULT_TAXONOMY_FIELDS
from skywatch.core.request.groups import DEFAULT_GROUP_FIELDS
from skywatch.core.request.store import DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, DEFAULT_SHOT_FIELDS

README = os.path.join(os.path.dirname(__file__), "..", "..", "README.md")

#: Each table's section and the code's default names by role.
DEFAULTS = {
    "TBL_IMAGING_REQUEST": DEFAULT_REQUEST_FIELDS,
    "TBL_REQUEST_HISTORY": DEFAULT_HISTORY_FIELDS,
    "TBL_REQUEST_GROUP": DEFAULT_GROUP_FIELDS,
    "TBL_REQUEST_SHOT": DEFAULT_SHOT_FIELDS,
    "TBL_OBSERVATION": DEFAULT_OBSERVATION_FIELDS,
    "TBL_OBSERVATION_AREA": DEFAULT_AREA_FIELDS,
    "TBL_TAXONOMY": DEFAULT_TAXONOMY_FIELDS,
    "TBL_ALERT_RULE": alerts.DEFAULT_RULE_FIELDS,
    "TBL_ALERT_GEOFENCE": alerts.DEFAULT_FENCE_FIELDS,
    "TBL_NAMED_AREA": alerts.DEFAULT_PLACE_FIELDS,
    "TBL_ALERT_LOG": alerts.DEFAULT_LOG_FIELDS,
    "TBL_COUNTRY": alerts.DEFAULT_COUNTRY_FIELDS,
    "TBL_ASSIGNMENT": DEFAULT_ASSIGNMENT_FIELDS,
    "TBL_HANDOVER_MEMO": DEFAULT_HANDOVER_FIELDS,
    "TBL_EVENT": events.DEFAULT_EVENT_FIELDS,
    "TBL_EVENT_LINK": events.DEFAULT_LINK_FIELDS,
}
NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")


def _section() -> str:
    with open(README, encoding="utf-8") as f:
        text = f.read()
    start = text.index("\n## Esri 側のテーブル定義\n")
    return text[start:text.index("\n## ", start + 1)]


def _tables(section: str) -> dict[str, list[list[str]]]:
    """Each `### \\`TBL_…\\`` section's table rows (cells without backticks)."""
    out: dict[str, list[list[str]]] = {}
    for part in re.split(r"\n### ", section)[1:]:
        m = re.match(r"`(TBL_[A-Z_]+)`", part)
        if not m:
            continue
        rows = [[c.strip().strip("`") for c in line.strip().strip("|").split("|")] for line in part.splitlines() if line.startswith("| `")]
        out[m.group(1)] = rows
    return out


def _slug(heading: str) -> str:
    """GitHub's anchor of a heading."""
    text = heading.strip().lower().replace("`", "")
    return "".join(c for c in text if c in "-_ " or unicodedata.category(c)[0] in "LMN").replace(" ", "-")


def test_every_default_attribute_is_listed_with_its_role():
    tables = _tables(_section())
    for table, defaults in DEFAULTS.items():
        rows = {r[-1]: r[0] for r in tables[table]}
        for role, name in defaults.items():
            assert rows.get(role) == name, f"{table} の {role}"


def test_names_follow_the_rule():
    section = _section()
    tables = _tables(section)
    assert len(tables) >= 20
    for table, rows in tables.items():
        assert NAME.match(table), table
        for row in rows:
            assert NAME.match(row[0]), f"{table}.{row[0]}"
            assert re.match(r"^(String\(\d+\)|Date|Double|Integer|SmallInteger)$", row[2]), f"{table}.{row[0]} の型 {row[2]}"
    for name in edit_stamp.DEFAULT_FIELDS.values():
        assert f"| `{name}` |" in section


def test_links_point_at_headings():
    section = _section()
    with open(README, encoding="utf-8") as f:
        anchors = {_slug(m.group(1)) for m in re.finditer(r"^#+ (.+)$", f.read(), re.MULTILINE)}
    for link in re.findall(r"\]\(#([^)]+)\)", section):
        assert link in anchors, link


def test_the_settings_example_reads_without_problems():
    from skywatch.core.config import parse_config_text
    example = _section().split("```jsonc\n", 1)[1].split("```", 1)[0]
    config, problems = parse_config_text(example)
    assert problems == []
    assert config.request_stores[0].history_fields["file"] == "OUTPUT_FILE"
    assert config.image_catalogs[0].fields["source"] == ["COG_URL", "FILE_PATH"]
