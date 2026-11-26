"""
よく使うボタンの強調 (tsudo, 2026-11-26: 「よく使うボタン(例えば画像カタログの
検索とか)を強調の意味で色付けたりしたい」, 3 段階: 無着色・少し強調・強調).

One hue only, blue, the levels told apart by fill and outline rather than by
colour, so that they read the same for every colour vision (P・D 型: blue stays
blue; T 型: it turns teal but keeps its lightness) and in black and white:

* STRONG (強調): blue fill, white bold text. What is pressed every time in a
  dock (画像カタログ「検索」「開く」, 観測記録「記録（Enter）」…), one or two a dock.
* LIGHT (少し強調): white, blue outline, blue bold text. Pressed often but not
  every time (「範囲を描く」, 「消失」…).
* Plain: the rest, as QGIS draws them. Orange (送信待ち, offline) and red
  (errors) keep their meaning of a warning; 削除 and the like stay plain.

A button that cannot be pressed (「開く」 before an image is chosen) loses its
colour and goes grey, so the colour also says it can be pressed. A checkable
one (「クリック記録」, 「確認範囲」) is darker while it is down.

The colours are here only: a dock marks its buttons with `emphasize(button,
STRONG)`. The button keeps the size QGIS gave it (its height, and its width
for the text it had; a long text in bold at most a few pixels wider), so
marking it moves nothing in the dock.
"""
from __future__ import annotations

from qgis.PyQt.QtWidgets import QAbstractButton, QToolButton

STRONG = "strong"
LIGHT = "light"
#: The dynamic property a marked button carries (its level), for the tests and the help's pictures.
PROPERTY = "mikageEmphasis"

BLUE = "#2563eb"
BLUE_DARK = "#1d4ed8"
NAVY = "#1e3a8a"
NAVY_DARK = "#172554"
PALE = "#eff6ff"
PALE_DOWN = "#dbeafe"
#: Cannot be pressed: the colours of a plain button that cannot be pressed.
GREY_FILL = "#f3f4f6"
GREY_LINE = "#d1d5db"
GREY_TEXT = "#9ca3af"

#: Each level's colours: (normal, hover, down/checked, focus line) as (background, border, text).
LEVELS = {
    STRONG: {
        "normal": (BLUE, BLUE_DARK, "#ffffff"),
        "hover": (BLUE_DARK, NAVY, "#ffffff"),
        "down": (NAVY, NAVY_DARK, "#ffffff"),
        "focus": NAVY_DARK,
    },
    LIGHT: {
        "normal": ("#ffffff", BLUE, BLUE_DARK),
        "hover": (PALE, BLUE, BLUE_DARK),
        "down": (PALE_DOWN, BLUE_DARK, NAVY),
        "focus": NAVY,
    },
}
#: Room inside the border at the sides (less when the bold text would make the button wider than QGIS's).
PAD_H = 8
PAD_H_LEAST = 2
BORDER = 1
#: The arrow part of a tool button with a menu (「不可 ▼」).
MENU_WIDTH = 14
#: The minimum size the button had before it was marked (given back when it is marked again).
_OWN_MINIMUM = "mikageEmphasisOwnMinimum"


def style_sheet(level: str, kind: str, pad: int = PAD_H, menu: bool = False) -> str:
    """The style sheet of a button of `kind` (QPushButton, QToolButton) at `level`, `pad` pixels inside at the sides."""
    c = LEVELS[level]

    def part(state: str, colours: tuple) -> str:
        background, border, text = colours
        return f"{kind}{state} {{ background: {background}; border-color: {border}; color: {text}; }}"

    background, border, text = c["normal"]
    # No room above and below: the height is QGIS's (the button's minimum height, see emphasize).
    rules = [
        f"{kind} {{ background: {background}; border: {BORDER}px solid {border}; border-radius: 3px; color: {text};"
        f" font-weight: bold; padding: 0px {pad + (MENU_WIDTH if menu else 0)}px 0px {pad}px; }}",
        part(":hover", c["hover"]),
        part(":pressed", c["down"]),
        part(":checked", c["down"]),
        f"{kind}:focus {{ border-color: {c['focus']}; }}",
        f"{kind}:disabled {{ background: {GREY_FILL}; border-color: {GREY_LINE}; color: {GREY_TEXT}; }}",
    ]
    if menu:
        rules += [
            f"{kind}::menu-button {{ border: none; border-left: {BORDER}px solid {border}; width: {MENU_WIDTH}px; }}",
            f"{kind}::menu-button:disabled {{ border-left-color: {GREY_LINE}; }}",
        ]
    return "\n".join(rules)


def emphasize(button: QAbstractButton, level: str = STRONG) -> QAbstractButton:
    """Colour `button` as a much used one (STRONG, LIGHT), at the size QGIS gives it; returns it."""
    if level not in LEVELS:
        raise ValueError(level)
    own = button.property(_OWN_MINIMUM)
    if own is None:
        button.setProperty(_OWN_MINIMUM, [button.minimumWidth(), button.minimumHeight()])
    else:
        # Marked again (its text changed…): measured as QGIS draws it, not as marked.
        button.setStyleSheet("")
        button.setMinimumSize(int(own[0]), int(own[1]))
    native = button.sizeHint()
    kind = "QToolButton" if isinstance(button, QToolButton) else "QPushButton"
    menu = isinstance(button, QToolButton) and button.popupMode() == QToolButton.ToolButtonPopupMode.MenuButtonPopup
    button.setProperty(PROPERTY, level)
    pad = PAD_H
    button.setStyleSheet(style_sheet(level, kind, pad, menu))
    # Bold text is wider: less room at the sides, so that the button is no wider than QGIS made it.
    wider = button.sizeHint().width() - native.width()
    if wider > 0:
        pad = max(PAD_H_LEAST, pad - (wider + 1) // 2)
        button.setStyleSheet(style_sheet(level, kind, pad, menu))
    # And no smaller (a short text: QGIS's buttons have a least width; the height is QGIS's).
    button.setMinimumSize(button.minimumSize().expandedTo(native))
    return button


def level_of(button: QAbstractButton) -> str:
    """STRONG, LIGHT or "" (plain)."""
    return button.property(PROPERTY) or ""
