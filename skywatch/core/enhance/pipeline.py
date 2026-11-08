"""
A chain of correction steps, as browser-image-enhancement's Pipeline: immutable,
`set` updates the first step of a kind in place (or appends it), and it saves
as `{"version": 1, "ops": [...]}` — the form of the viewer's projects and
`imageRules[].enhance`, so the same JSON moves between them.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional, Union

from .ops import OP_INFO, is_identity, margin_of, normalize_op


class Pipeline:
    def __init__(self, ops: Iterable[Any] = ()):
        self.ops: tuple = tuple(normalize_op(op) for op in ops)

    def __repr__(self) -> str:
        return f"Pipeline({list(self.ops)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Pipeline) and self.ops == other.ops

    def __hash__(self) -> int:
        return hash(json.dumps(self.to_json(), sort_keys=True))

    def add(self, op: str, params: Union[dict, float, None] = None) -> "Pipeline":
        """A pipeline with the step appended."""
        return Pipeline([*self.ops, {**self._given(op, params), "op": op}])

    def set(self, op: str, params: Union[dict, float, None] = None) -> "Pipeline":
        """The first step of kind `op` updated (parameters not given keep their values), or appended."""
        given = self._given(op, params)
        ops = list(self.ops)
        for i, s in enumerate(ops):
            if s["op"] == op:
                ops[i] = {**s, **given, "op": op}
                return Pipeline(ops)
        return Pipeline([*ops, {**given, "op": op}])

    def get(self, op: str) -> Optional[dict]:
        """The first step of kind `op`, or None."""
        return next((dict(s) for s in self.ops if s["op"] == op), None)

    def remove(self, op: str) -> "Pipeline":
        return Pipeline([s for s in self.ops if s["op"] != op])

    @property
    def is_identity(self) -> bool:
        return all(is_identity(op) for op in self.ops)

    @property
    def margin(self) -> int:
        """Pixels of context each side a tile needs (sharpen)."""
        return margin_of(self.ops)

    @property
    def needs_stats(self) -> bool:
        return any(op["op"] == "autoStretch" for op in self.ops)

    def scaled(self, factor: float) -> "Pipeline":
        """The same correction for the image shrunk by `factor`: the sharpen radius scales with it."""
        if not factor > 0 or factor == 1:
            return self
        return Pipeline({**op, "radius": min(50.0, max(0.1, op["radius"] * factor))} if op["op"] == "sharpen" else op for op in self.ops)

    def to_json(self) -> dict:
        return {"version": 1, "ops": [dict(op) for op in self.ops]}

    def dumps(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False)

    @staticmethod
    def from_json(value: Union[str, dict, list]) -> "Pipeline":
        """From `{"version": 1, "ops": [...]}` (object or text), or a bare list of steps. Raises ValueError otherwise."""
        parsed = json.loads(value) if isinstance(value, str) else value
        if isinstance(parsed, list):
            return Pipeline(parsed)
        if not isinstance(parsed, dict) or parsed.get("version") != 1 or not isinstance(parsed.get("ops"), list):
            raise ValueError("Not a correction (expected { version: 1, ops: [...] }).")
        return Pipeline(parsed["ops"])

    @staticmethod
    def _given(op: str, params) -> dict:
        info = OP_INFO.get(op)
        if info is None:
            raise ValueError(f"Unknown correction: {op}")
        if isinstance(params, (int, float)) and not isinstance(params, bool):
            if not info["value"]:
                raise ValueError(f"{op} takes an object of parameters, not a number.")
            return {info["value"]: float(params)}
        return dict(params or {})


#: The built-in corrections (presets.ts), by name.
PRESETS: dict[str, Pipeline] = {
    "auto": Pipeline([{"op": "autoStretch"}, {"op": "sharpen", "amount": 0.3}]),
    "vivid": Pipeline([{"op": "autoStretch"}, {"op": "contrast", "amount": 0.12}, {"op": "saturation", "amount": 0.25},
                       {"op": "sharpen", "amount": 0.4}]),
    "soft": Pipeline([{"op": "shadows", "amount": 0.4}, {"op": "highlights", "amount": -0.3}, {"op": "contrast", "amount": -0.1},
                      {"op": "temperature", "amount": 0.08}]),
    "satellite": Pipeline([{"op": "autoStretch", "lowPercent": 2, "highPercent": 2}, {"op": "sharpen", "amount": 0.6}]),
    "document": Pipeline([{"op": "autoStretch", "linked": True, "lowPercent": 1, "highPercent": 10}, {"op": "contrast", "amount": 0.3},
                          {"op": "sharpen", "amount": 0.8, "radius": 1.5}]),
    "blackAndWhite": Pipeline([{"op": "autoStretch", "linked": True}, {"op": "saturation", "amount": -1},
                               {"op": "contrast", "amount": 0.15}]),
}
PRESET_NAMES = {
    "auto": "自動", "vivid": "鮮やか", "soft": "やわらか", "satellite": "衛星画像", "document": "文書", "blackAndWhite": "白黒",
}


def enhance_of(enhance: Any, base: Optional[Pipeline], problems: list, at: str) -> Optional[Pipeline]:
    """
    The correction an `imageRules[].enhance` gives, as the viewer's config.ts: a
    saved pipeline (or a list of steps) replaces `base`; an object of steps
    (`{"contrast": 0.2, "autoStretch": {"lowPercent": 1}}`) is set on top of it.
    """
    if isinstance(enhance, list) or (isinstance(enhance, dict) and "ops" in enhance):
        try:
            return Pipeline.from_json(enhance)
        except ValueError as error:
            problems.append(f"{at} の enhance は補正として読み込み不可（{error}）")
            return base
    if not isinstance(enhance, dict):
        problems.append(f"{at} の enhance はオブジェクトで指定")
        return base
    p = base or Pipeline()
    for op, params in enhance.items():
        if not (isinstance(params, dict) or (isinstance(params, (int, float)) and not isinstance(params, bool))):
            problems.append(f"{at} の enhance の「{op}」は数値か、パラメーターのオブジェクトで指定")
            continue
        try:
            p = p.set(op, params)
        except ValueError as error:
            problems.append(f"{at} の enhance の「{op}」は無視（{error}）")
    return p
