"""
Builds the help page mikage/help/index.html from its parts in mikage/help/src/
(one file per section, joined in the order of their names), so a change to one
section touches one small file (2026-11-20, docs/architecture.md).

    python3 scripts/build_help.py          # writes index.html (and docs/admin/error-codes.md)
    python3 scripts/build_help.py --check  # exit 1 when either is not what the sources make

The page stays one HTML file with nothing from outside: it opens as it is in
a browser and in the plugin's 「使い方（ヘルプ）」. Edit the parts, never
index.html; when two branches both changed the help, take either index.html
and run this again.

The section 「エラーコード一覧」 (src/105-errors.html) has its tables made here
from mikage/core/errors.py (CODES, KINDS) where the part says
`<!-- error-codes -->`, and so does docs/admin/error-codes.md (with what the
administrator looks at): a code changed in errors.py, then this run again.
"""
from __future__ import annotations

import html
import importlib.util
import os
import sys

HELP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mikage", "help")
SRC = os.path.join(HELP, "src")
PAGE = os.path.join(HELP, "index.html")
ERRORS = os.path.join(HELP, os.pardir, "core", "errors.py")
ADMIN_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "admin", "error-codes.md")
#: Where the part 「エラーコード一覧」 takes its tables.
MARK = "<!-- error-codes -->"


def parts() -> list[str]:
    return sorted(f for f in os.listdir(SRC) if f.endswith(".html"))


def errors_module():
    """mikage/core/errors.py on its own (it imports nothing of the plugin's)."""
    spec = importlib.util.spec_from_file_location("mikage_errors", os.path.normpath(ERRORS))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # (its dataclass looks itself up there)
    spec.loader.exec_module(module)
    return module


def _by_kind(errors) -> list[tuple[str, str, list]]:
    """(anchor, title, codes) of each kind of cause that has codes, in the order of the codes."""
    out = []
    for digit, (anchor, title) in errors.KINDS.items():
        codes = [c for c in errors.CODES.values() if c.code[1] == digit]
        if codes:
            out.append((anchor, title, codes))
    return out


def code_tables(errors=None) -> str:
    """The tables of 「エラーコード一覧」: a subsection (h3) per kind, a row per code (its anchor is the code)."""
    errors = errors or errors_module()
    e = html.escape
    out = []
    for anchor, title, codes in _by_kind(errors):
        out.append(f'<h3 id="{anchor}">{e(title)}</h3>\n<table class="codes">\n'
                   "<tr><th>コード</th><th>画面の表示</th><th>原因</th><th>対処</th></tr>\n")
        for c in codes:
            out.append(f'<tr><td class="code"><a name="{c.code}">{c.code}</a></td><td>{e(c.text)}</td><td>{e(c.cause)}</td><td>{e(c.remedy)}</td></tr>\n')
        out.append("</table>\n")
    if errors.RETIRED:
        out.append("<p>使われなくなったコード（ほかの原因には使いません）: "
                   + "、".join(f"{e(k)}（{e(v)}）" for k, v in sorted(errors.RETIRED.items())) + "</p>\n")
    return "".join(out)


def admin_doc(errors=None) -> str:
    """docs/admin/error-codes.md: every code with what the administrator looks at."""
    errors = errors or errors_module()

    def cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    out = ["# エラーコード一覧（管理者向け）\n\n",
           "> このファイルは `python3 scripts/build_help.py` が `mikage/core/errors.py` から作ります。直接は直さないでください。\n\n",
           "画面の失敗の表示は「〇〇に失敗: 理由［E101］」の形です。利用者向けの原因と対処はヘルプ（メニュー「使い方（ヘルプ）…」の"
           "「エラーコード一覧」。検索欄にコードを入れると、その行に飛びます）にあります。ここには、管理者が確かめるところを足しています。\n\n",
           "- サーバーの URL・サーバー自身のメッセージ・Python のエラーは画面に出さず、ログメッセージの「MIKAGE」タブと監査ログに残します。\n",
           "- 監査ログでは、エラーの行の `detail.code` にコードが入ります（列は増やしていません。ファイルの形式とハッシュの鎖をそのままにするため）。"
           "原因ごとの件数は `detail.code` で数えられます。\n",
           "- コードは別の原因に使い回しません。使わなくなったコードは `RETIRED` に残します。\n\n"]
    for _anchor, title, codes in _by_kind(errors):
        out.append(f"## {title}\n\n| コード | 画面の表示 | 原因 | 対処（利用者） | 管理者が確かめること |\n|---|---|---|---|---|\n")
        for c in codes:
            out.append(f"| {c.code} | {cell(c.text)} | {cell(c.cause)} | {cell(c.remedy)} | {cell(c.admin)} |\n")
        out.append("\n")
    if errors.RETIRED:
        out.append("## 使われなくなったコード\n\n" + "".join(f"- {k}: {v}\n" for k, v in sorted(errors.RETIRED.items())) + "\n")
    return "".join(out).rstrip("\n") + "\n"


def build() -> str:
    out = []
    for name in parts():
        with open(os.path.join(SRC, name), encoding="utf-8", newline="") as f:
            text = f.read()
        if MARK in text:
            text = text.replace(MARK, MARK + "\n" + code_tables(), 1)
        out.append(text)
    return "".join(out)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except OSError:
        return ""


def main(argv: list[str]) -> int:
    page, doc = build(), admin_doc()
    if "--check" in argv:
        same = True
        if _read(PAGE) != page:
            print("mikage/help/index.html is not built from mikage/help/src: run python3 scripts/build_help.py")
            same = False
        if _read(ADMIN_DOC) != doc:
            print("docs/admin/error-codes.md is not built from mikage/core/errors.py: run python3 scripts/build_help.py")
            same = False
        return 0 if same else 1
    with open(PAGE, "w", encoding="utf-8", newline="") as f:
        f.write(page)
    with open(ADMIN_DOC, "w", encoding="utf-8", newline="") as f:
        f.write(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
