"""
「撮像計画ファイル出力」 with a system given each row's 外部連携ID (`linkId`,
2026-11-12): the file and the history hold the same short id, and a history
table without LINK_ID is told of.
"""
import copy
import csv
import io
import json

import pytest

pytest.importorskip("qgis.core")

from mikage.tests import test_qgis_requests as base  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402

plugin = base.plugin


def _write(p, tmp_path, out_name: str):
    daily = p.api.plan_files
    daily.show()
    out = tmp_path / out_name
    out.mkdir()
    daily.folder.setText(str(out))
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    daily._confirm = lambda lines, earlier, label="": False
    return daily, out


def test_the_file_and_the_history_hold_the_same_id(plugin):
    p, _iface, fake, tmp_path = plugin
    config = copy.deepcopy(base.CONFIG)
    sys_a = config["requestExports"][0]
    sys_a.update(limit={"count": 10}, linkIdLength=10, columns=[*sys_a["columns"], {"key": "REF", "source": "linkId", "format": "SW-%s"}])
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.api.reload_config()
    assert wait_for(lambda: p.api.config.request_exports[0].link_length == 10), p.api.problems

    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    dock.import_rows(base.fixed_layer(), ask=False)
    assert wait_for(lambda: dock.task is None and len(dock.requests) == 2)

    daily, out = _write(p, tmp_path, "out")
    daily.write()
    assert wait_for(lambda: daily.task is None and len(fake.layers[base.HISTORY].rows) == 2), daily.status.text()
    [path] = daily.paths
    rows = list(csv.DictReader(io.StringIO(open(path, encoding="cp932").read())))
    kept = {r["attributes"]["REQ_ID"]: r["attributes"]["LINK_ID"] for r in fake.layers[base.HISTORY].rows}
    assert {r["ID"]: r["REF"] for r in rows} == {k: f"SW-{v}" for k, v in kept.items()}
    assert all(len(v) == 10 for v in kept.values()) and "LINK_ID" not in daily.status.text()

    # A history table made before LINK_ID: the files are written, and the person is told the ids are not kept.
    fake.layers[base.HISTORY].types.pop("LINK_ID")
    daily.write()
    assert wait_for(lambda: daily.task is None and "LINK_ID" in daily.status.text()), daily.status.text()
    assert "出力履歴に属性 LINK_ID がないため外部連携IDを保存できず（tools/arcgis/create_tables.py で足す）" in daily.status.text()
