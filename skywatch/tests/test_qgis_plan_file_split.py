"""
「撮像計画ファイル出力」 with a system that takes at most so many requests
in one file (`maxPerFile`, tsudo 2026-11-11): the day written in numbered
files, the history naming each request's file, and the numbered files of
an earlier writing deleted.
"""
import copy
import csv
import io
import json
import os
import time

import pytest

pytest.importorskip("qgis.core")

from skywatch.core.request.model import jst_day  # noqa: E402
from skywatch.tests import test_qgis_requests as base  # noqa: E402
from skywatch.tests.qgis_fakes import wait_for  # noqa: E402

plugin = base.plugin


def test_the_day_split_into_files(plugin):
    p, _iface, fake, tmp_path = plugin
    config = copy.deepcopy(base.CONFIG)
    sys_a = config["requestExports"][0]
    sys_a.update(limit={"count": 10}, maxPerFile=1)
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.api.reload_config()
    assert wait_for(lambda: p.api.config.request_exports[0].max_per_file == 1), p.api.problems

    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    dock.import_rows(base.fixed_layer(), ask=False)
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 2)

    day = jst_day(time.time() * 1000).strftime("%Y%m%d")
    out = tmp_path / "out"
    out.mkdir()
    # Left from earlier writings of the day (unsplit, and in three); another day's and another name stay.
    for name in (f"SYS-A_{day}.csv", f"SYS-A_{day}_3.csv", f"SYS-A_20000101_3.csv", f"SYS-A_{day}_memo.txt"):
        (out / name).write_text("old", encoding="utf-8")
    daily = p.api.plan_files
    daily.show()
    daily.folder.setText(str(out))
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    assert {c.request.name: (c.result, c.system.system) for c in daily.plan.candidates} == {"横須賀": ("採用", "SYS-A"), "呉": ("採用", "SYS-A")}

    # The question before writing: each file with its number, and the files to delete.
    asked = []
    daily._confirm = lambda lines, earlier, label="": asked.append(lines) or False
    daily.write()
    assert wait_for(lambda: daily.task is None and len(fake.layers[base.HISTORY].rows) == 2), daily.status.text()
    names = sorted(os.path.basename(x) for x in daily.paths)
    assert names == [f"SYS-A_{day}_1.csv", f"SYS-A_{day}_2.csv"]
    lines = asked[0]
    assert lines[0] == f"A（1/2）: {out / names[0]}（1 件）" and lines[1] == f"A（2/2）: {out / names[1]}（1 件）"
    assert lines[-1] == f"前の書き出しで残っている番号違いのファイル 2 件を削除: SYS-A_{day}.csv、SYS-A_{day}_3.csv"
    for name in names:
        rows = list(csv.reader(io.StringIO((out / name).read_text(encoding="cp932"))))
        assert rows[0] == ["ID", "名称", "日", "優先度"] and len(rows) == 2
    # Higher first: 横須賀 (300) in the first file, 呉 (320) in the second; the history says which.
    first = list(csv.reader(io.StringIO((out / names[0]).read_text(encoding="cp932"))))
    assert first[1][1] == "横須賀"
    rows = {r["attributes"]["REQ_NAME"]: r["attributes"] for r in fake.layers[base.HISTORY].rows}
    assert {n: (a["RESULT"], a["OUTPUT_FILE"]) for n, a in rows.items()} == {"横須賀": ("採用", names[0]), "呉": ("採用", names[1])}
    assert sorted(x.name for x in out.iterdir()) == sorted([*names, "SYS-A_20000101_3.csv", f"SYS-A_{day}_memo.txt"])
    status = daily.status.text()
    assert "A 2 件（2 ファイル）" in status and "番号違いのファイル 2 件を削除" in status
