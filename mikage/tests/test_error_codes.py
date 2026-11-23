"""
エラーコード (core/errors.py, tsudo 2026-11-23): the list holds together, every
code is used and every code used is listed, the help and the administrators'
page are made from it, and errors turn into the screen's text and the log's.
"""
import errno
import importlib.util
import os
import re

from mikage.core import errors

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
#: A code where the plugin uses one: a class's `code = "E101"`, `code="E101"`, a literal 「［E101］」 or "E101" in the source.
USED = re.compile(r'"(E\d{3})"|［(E\d{3})］')


def _build_help():
    spec = importlib.util.spec_from_file_location("build_help", os.path.join(REPO, "scripts", "build_help.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sources():
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "ext", "__pycache__")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(folder, name)


def _used() -> dict[str, list[str]]:
    """Every code the plugin's source names, with where (the list's own definitions left out)."""
    out: dict[str, list[str]] = {}
    for path in _sources():
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if path.endswith(os.path.join("core", "errors.py")) and line.lstrip().startswith("Code("):
                    continue
                for a, b in USED.findall(line):
                    out.setdefault(a or b, []).append(f"{os.path.relpath(path, ROOT)}:{n}")
    return out


def test_the_list_holds_together():
    codes = [c.code for c in errors._CODES]
    assert len(codes) == len(set(codes)) and codes == sorted(codes)
    assert not set(codes) & set(errors.RETIRED)
    for c in errors.CODES.values():
        assert re.fullmatch(r"E\d{3}", c.code) and c.code[1] in errors.KINDS, c.code
        # The screen's words: 言い切り, no final 「。」, short; the help's: です・ます sentences.
        assert c.text and not c.text.endswith("。") and len(c.text) <= 24 and "［" not in c.text, c.code
        assert c.cause.endswith("。") and c.remedy.endswith("。"), c.code
        # Nothing the administrator alone needs on the person's screen (table names, server internals).
        assert "TBL_" not in c.text + c.cause + c.remedy, c.code
    anchors = [a for a, _t in errors.KINDS.values()]
    assert len(anchors) == len(set(anchors)) and all(a.startswith("errors-") for a in anchors)


def test_every_code_is_used_and_every_code_used_is_listed():
    used = _used()
    unknown = {code: where for code, where in used.items() if code not in errors.CODES}
    assert unknown == {}, unknown
    assert sorted(set(errors.CODES) - set(used)) == []


def test_the_help_and_the_administrators_page_are_made_from_the_list():
    build_help = _build_help()
    with open(build_help.PAGE, encoding="utf-8", newline="") as f:
        page = f.read()
    assert page == build_help.build()
    # Every code a row of its own (its anchor), under its kind's heading, with its text, cause and remedy.
    section = page.split('<h2 id="errors">', 1)[1].split("<h2 ", 1)[0]
    for c in errors.CODES.values():
        assert section.count(f'<a name="{c.code}">{c.code}</a>') == 1, c.code
        row = section.split(f'<a name="{c.code}">', 1)[1].split("</tr>", 1)[0]
        assert c.text in row and c.cause in row and c.remedy in row, c.code
    for digit, (anchor, title) in errors.KINDS.items():
        if any(code[1] == digit for code in errors.CODES):
            assert f'<h3 id="{anchor}">{title}</h3>' in section
    # The administrators' page: the same codes, with where to look; linked from the README.
    with open(build_help.ADMIN_DOC, encoding="utf-8", newline="") as f:
        doc = f.read()
    assert doc == build_help.admin_doc()
    for c in errors.CODES.values():
        assert f"| {c.code} | {c.text} |" in doc
    assert build_help.main(["--check"]) == 0


def test_texts_carry_their_code_once():
    assert errors.tagged("記録の取得に失敗: サーバーに接続できない", "E101") == "記録の取得に失敗: サーバーに接続できない［E101］"
    assert errors.tagged("サーバーに接続できない［E101］", "E101") == "サーバーに接続できない［E101］"
    assert errors.tagged("名前を入力", "") == "名前を入力"
    assert errors.code_of("…に失敗: サーバーに接続できない［E101］") == "E101" and errors.code_of("名前を入力") is None
    assert errors.untagged("サーバーに接続できない［E101］") == "サーバーに接続できない"
    for typed in ("E101", "e101", "Ｅ１０１", "E-101", "[E101]", "［E101］", " e 101 "):
        assert errors.normalized(typed) == "E101", typed
    for typed in ("", "101", "E10", "E1011", "errors", "サーバー"):
        assert errors.normalized(typed) is None, typed


def test_a_coded_error_shows_its_text_and_keeps_the_details_for_the_log():
    error = errors.Failed(code="E101", detail="https://gis.example/arcgis：Connection refused")
    assert str(error) == "サーバーに接続できない［E101］" and error.code == "E101" and error.message == "サーバーに接続できない"
    assert "example" not in str(error)
    assert errors.describe(error) == ("サーバーに接続できない［E101］", "https://gis.example/arcgis：Connection refused")
    assert errors.detailed(error).startswith("サーバーに接続できない") and "Connection refused" in errors.detailed(error)
    # Wrapped again with what was being done: the code stays at the end once.
    again = errors.Failed(f"表の取得に失敗: {error.message}", code=error.code)
    assert str(again) == "表の取得に失敗: サーバーに接続できない［E101］"
    # Guidance has no code.
    assert str(errors.Failed("名前を入力", code="")) == "名前を入力"
    assert isinstance(errors.FileFailed(code="E402"), OSError) and isinstance(errors.Failed(code="E101"), ValueError)


def test_python_s_own_errors_become_a_code_and_a_detail():
    shown, detail = errors.describe(FileNotFoundError(errno.ENOENT, "No such file or directory", "C:/data/a.tif"))
    assert shown == "ファイル・フォルダがない［E401］" and detail == "C:/data/a.tif: No such file or directory"
    assert errors.describe(PermissionError(errno.EACCES, "Permission denied", "a.csv"))[0] == "ファイルに書き込めない（使用中・権限）［E402］"
    assert errors.describe(OSError(errno.ENOSPC, "No space left on device"))[0] == "ディスクの空きがない［E403］"
    assert errors.describe(OSError(errno.EIO, "Input/output error"))[0].endswith("［E407］")
    assert errors.describe(ImportError("No module named 'numpy'"))[0] == "プラグインの部品が足りない［E902］"
    shown, detail = errors.describe(KeyError("TITLE"))
    assert shown == "想定外のエラー［E901］" and detail == "KeyError: 'TITLE'"
    # The plugin's own words (guidance) as they are.
    assert errors.describe(ValueError("名前を入力")) == ("名前を入力", "")
