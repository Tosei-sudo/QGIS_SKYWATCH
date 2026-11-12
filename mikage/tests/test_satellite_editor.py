"""
tools/satellite_editor.html (the satellite, sensor, alias and annual quota table editor)
carries a copy of the plugin's field roles and defaults, and reads the tables
the way the plugin does. This checks the copy is current and that every role
has an input.

To update it after changing the plugin:

    python3 -m mikage.tests.test_satellite_editor
"""
import json
import os
import re

HTML = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "satellite_editor.html")
BLOCK = re.compile(r'(<script type="application/json" id="plugin-constants">\n)(.*?)(\n</script>)', re.S)


def plugin_constants() -> dict:
    """What the tool needs from the plugin, from the plugin's own definitions."""
    from mikage.core import edit_stamp, time_zone
    from mikage.core.plan import modes, names, satellites
    from mikage.core.request import store

    return {
        "satelliteRoles": list(satellites.SATELLITE_ROLES),
        "satelliteDefaultKeys": list(satellites.DEFAULT_KEYS),
        "satelliteDefaults": dict(satellites.BUILT_IN_DEFAULTS),
        "sensorRoles": list(modes.SENSOR_ROLES),
        "sensorNumberRoles": list(modes.NUMBER_ROLES),
        "aliasRoles": list(names.ALIAS_ROLES),
        "quotaRoles": list(store.DEFAULT_QUOTA_FIELDS),
        "quotaFields": dict(store.DEFAULT_QUOTA_FIELDS),
        "editTrackingFields": dict(edit_stamp.DEFAULT_FIELDS),
        "editTrackingUpdateRoles": list(edit_stamp.UPDATE_ROLES),
        "timeZoneSources": list(time_zone.TIME_ZONE_SOURCES),
    }


def _read():
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _object(text: str, name: str) -> str:
    """The text of `const NAME = { … };` in the page's script."""
    start = text.index(f"const {name} = {{")
    return text[start:text.index("\n};", start)]


def test_constants_match_the_plugin():
    m = BLOCK.search(_read())
    assert m, "plugin-constants の script がない"
    assert json.loads(m.group(2)) == plugin_constants(), (
        "tools/satellite_editor.html の定数が古い: python3 -m mikage.tests.test_satellite_editor で更新")


def test_every_role_has_an_input():
    from mikage.core.plan import modes, names, satellites
    from mikage.core.request.store import DEFAULT_QUOTA_FIELDS

    text = _read()
    for table, roles in (("SAT_INFO", satellites.SATELLITE_ROLES), ("SENSOR_INFO", modes.SENSOR_ROLES), ("ALIAS_INFO", names.ALIAS_ROLES),
                         ("QUOTA_INFO", DEFAULT_QUOTA_FIELDS)):
        block = _object(text, table)
        for role in roles:
            assert re.search(rf"\b{role}: \{{ label: ", block), f"{table} に {role} がない"
    # Every role is placed in a group of the forms (the TLE roles share one box).
    groups = text[text.index("const SAT_GROUPS = ["):text.index("const SENSOR_GROUPS = [")]
    for role in satellites.SATELLITE_ROLES:
        assert f'"{"tle" if role.startswith("tle") else role}"' in groups, f"SAT_GROUPS に {role} がない"
    groups = text[text.index("const SENSOR_GROUPS = ["):text.index("const DATE_TYPES")]
    for role in modes.SENSOR_ROLES:
        assert f'"{role}"' in groups, f"SENSOR_GROUPS に {role} がない"


def test_the_number_roles_are_checked_as_numbers():
    from mikage.core.plan import modes, satellites

    text = _read()
    numbers = set(modes.NUMBER_ROLES) | set(satellites.DEFAULT_KEYS) | {"length", "resolution"}
    for table in ("SAT_INFO", "SENSOR_INFO"):
        block = _object(text, table)
        for role in numbers:
            m = re.search(rf"\b{role}: \{{[^}}]*\}}", block)
            assert m and "num: true" in m.group(0), f"{table}.{role} が数の入力でない"
    block = _object(text, "QUOTA_INFO")
    for role in ("limit", "startMonth", "initialUsed"):
        m = re.search(rf"\b{role}: \{{[^}}]*\}}", block)
        assert m and "num: true" in m.group(0) and "int: true" in m.group(0), f"QUOTA_INFO.{role} が整数の入力でない"


def test_the_page_loads_nothing_from_outside():
    text = _read()
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|@import|https?://(?!gis\.example)[^\s\"'<>]+\.(js|css)", text)


def write():
    text = _read()
    block = json.dumps(plugin_constants(), ensure_ascii=False, indent=1)
    new = BLOCK.sub(lambda m: m.group(1) + block + m.group(3), text) if BLOCK.search(text) else text
    with open(HTML, "w", encoding="utf-8") as f:
        f.write(new)


if __name__ == "__main__":
    write()
    print("updated", os.path.normpath(HTML))
