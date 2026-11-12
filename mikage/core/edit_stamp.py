"""
Who added or changed a feature, and when: every Esri layer and table the
plugin writes to gets the time and the user name in its tracking fields
(the settings file's `editTracking`), on adding (all four) and on updating
(the two "updated" ones).

The user is the operating system's user (the environment variable
`USERNAME`, or the one `userVariable` names), not the Esri sign-in: the
same person signs in with a shared account on some networks. `user` in the
settings file sets it outright.

A layer gets only the fields it has (names compared without case), so a
layer without them is written as before. The default names are not Esri's
own editor tracking fields (`CreationDate`, `Creator` … / `created_user`
…), which the server fills with the sign-in and would overwrite.

The settings are module state (like time_zone's rules): set when the
settings file is read, used by core/esri.py `apply_edits` and by the layers
QGIS edits itself (qgis_io/edit_stamp_layers.py).
"""
from __future__ import annotations

import getpass
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .util import iso_text

#: The roles of the tracking fields and their default names.
DEFAULT_FIELDS = {
    "createdAt": "CREATED_DATE",
    "createdBy": "CREATED_USER",
    "updatedAt": "UPDATED_DATE",
    "updatedBy": "UPDATED_USER",
}
ROLES = tuple(DEFAULT_FIELDS)
#: The roles written when a feature is added, and when one is updated.
ADD_ROLES = ROLES
UPDATE_ROLES = ("updatedAt", "updatedBy")
DEFAULT_USER_VARIABLE = "USERNAME"

_DATE_TYPES = {"esriFieldTypeDate", "esriFieldTypeTimestampOffset"}
_TEXT_TYPES = {"esriFieldTypeString", "esriFieldTypeGUID", "esriFieldTypeGlobalID"}


@dataclass(frozen=True)
class EditTracking:
    """The settings file's `editTracking`."""
    enabled: bool = True
    #: The environment variable the user name is read from.
    user_variable: str = DEFAULT_USER_VARIABLE
    #: The user name written, whatever the environment says ("": from the environment).
    user: str = ""
    #: Role → field name; a role left out (or "") is not written.
    fields: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_FIELDS))


DEFAULT_EDIT_TRACKING = EditTracking()

_current: EditTracking = DEFAULT_EDIT_TRACKING


def set_tracking(tracking: Optional[EditTracking]) -> None:
    """Sets the settings in use (None: the defaults)."""
    global _current
    _current = tracking or DEFAULT_EDIT_TRACKING


def tracking() -> EditTracking:
    return _current


def user_name(t: Optional[EditTracking] = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """
    The user written: `user`, else the environment variable, else (that
    variable unset) the login name the system knows ("" when there is none).
    """
    t = t or _current
    if t.user.strip():
        return t.user.strip()
    env = os.environ if environ is None else environ
    value = (env.get(t.user_variable) or "").strip() if t.user_variable else ""
    if value:
        return value
    if environ is not None:
        return ""
    try:
        return getpass.getuser().strip()
    except Exception:  # noqa: BLE001 (no user known: none written)
        return ""


def now_ms() -> int:
    return int(time.time() * 1000)


def tracked_fields(field_types: Mapping[str, str], t: Optional[EditTracking] = None) -> dict[str, tuple[str, str]]:
    """Role → (the layer's own name of the field, its Esri type), for the roles the layer has."""
    t = t or _current
    if not t.enabled:
        return {}
    by_lower = {name.lower(): (name, kind) for name, kind in field_types.items()}
    out = {}
    for role in ROLES:
        name = (t.fields.get(role) or "").strip()
        if name and name.lower() in by_lower:
            out[role] = by_lower[name.lower()]
    return out


def _value(role: str, kind: str, when: int, user: str) -> Any:
    if role.endswith("By"):
        return user if kind in _TEXT_TYPES or not kind else None
    if kind in _DATE_TYPES or not kind:
        return when
    if kind in _TEXT_TYPES:
        return iso_text(when)
    if "Double" in kind or "Integer" in kind or "Big" in kind:
        return when
    return None


def stamp_values(fields: Mapping[str, tuple[str, str]], adding: bool, when: int, user: str) -> dict[str, Any]:
    """Field name → value of the tracking fields an added (`adding`) or updated feature gets."""
    out = {}
    for role in ADD_ROLES if adding else UPDATE_ROLES:
        if role not in fields:
            continue
        name, kind = fields[role]
        value = _value(role, kind, when, user)
        if value is not None and not (role.endswith("By") and not value):
            out[name] = value
    return out


def stamp(features: list[dict[str, Any]], field_types: Mapping[str, str], adding: bool,
          when: Optional[int] = None, t: Optional[EditTracking] = None, user: Optional[str] = None) -> list[dict[str, Any]]:
    """The features (applyEdits adds / updates) with their tracking fields set; the same list when the layer has none."""
    fields = tracked_fields(field_types, t)
    if not fields or not features:
        return features
    values = stamp_values(fields, adding, now_ms() if when is None else when, user_name(t) if user is None else user)
    if not values:
        return features
    out = []
    for f in features:
        attributes = dict(f.get("attributes") or {})
        attributes.update(values)
        out.append({**f, "attributes": attributes})
    return out


def edit_tracking_of(value: Any, problems: list[str]) -> EditTracking:
    """
    The settings file's `editTracking`: false (none written), or an object
    `{ "userVariable": "USERNAME", "user": "", "fields": { "createdAt": …,
    "createdBy": …, "updatedAt": …, "updatedBy": … } }` (each key optional;
    a role set to "" or null is not written).
    """
    if value is False:
        return EditTracking(enabled=False)
    if value is True or value is None:
        return DEFAULT_EDIT_TRACKING
    if not isinstance(value, dict):
        problems.append("editTracking は false かオブジェクトで指定")
        return DEFAULT_EDIT_TRACKING
    enabled = True
    if "enabled" in value:
        if isinstance(value["enabled"], bool):
            enabled = value["enabled"]
        else:
            problems.append("editTracking の enabled は true か false で指定")
    variable = DEFAULT_USER_VARIABLE
    if "userVariable" in value:
        v = value["userVariable"]
        if isinstance(v, str) and v.strip():
            variable = v.strip()
        else:
            problems.append("editTracking の userVariable は環境変数名（文字列）で指定")
    user = ""
    if "user" in value and value["user"] is not None:
        if isinstance(value["user"], str):
            user = value["user"].strip()
        else:
            problems.append("editTracking の user は文字列で指定")
    fields = dict(DEFAULT_FIELDS)
    if "fields" in value:
        given = value["fields"]
        if not isinstance(given, dict):
            problems.append("editTracking の fields はオブジェクトで指定")
        else:
            for role, name in given.items():
                if role not in DEFAULT_FIELDS:
                    problems.append(f"editTracking の fields に不明な役割「{role}」（{', '.join(ROLES)}）")
                elif name is None or name == "":
                    fields[role] = ""
                elif isinstance(name, str):
                    fields[role] = name.strip()
                else:
                    problems.append(f"editTracking の fields.{role} は属性名（文字列）で指定")
    return EditTracking(enabled=enabled, user_variable=variable, user=user, fields=fields)
