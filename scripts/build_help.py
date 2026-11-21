"""
Builds the help page mikage/help/index.html from its parts in mikage/help/src/
(one file per section, joined in the order of their names), so a change to one
section touches one small file (2026-11-20, docs/architecture.md).

    python3 scripts/build_help.py          # writes index.html
    python3 scripts/build_help.py --check  # exit 1 when index.html is not what the parts make

The page stays one HTML file with nothing from outside: it opens as it is in
a browser and in the plugin's 「使い方（ヘルプ）」. Edit the parts, never
index.html; when two branches both changed the help, take either index.html
and run this again.
"""
from __future__ import annotations

import os
import sys

HELP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mikage", "help")
SRC = os.path.join(HELP, "src")
PAGE = os.path.join(HELP, "index.html")


def parts() -> list[str]:
    return sorted(f for f in os.listdir(SRC) if f.endswith(".html"))


def build() -> str:
    out = []
    for name in parts():
        with open(os.path.join(SRC, name), encoding="utf-8", newline="") as f:
            out.append(f.read())
    return "".join(out)


def main(argv: list[str]) -> int:
    page = build()
    if "--check" in argv:
        with open(PAGE, encoding="utf-8", newline="") as f:
            same = f.read() == page
        if not same:
            print("mikage/help/index.html is not built from mikage/help/src: run python3 scripts/build_help.py")
        return 0 if same else 1
    with open(PAGE, "w", encoding="utf-8", newline="") as f:
        f.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
