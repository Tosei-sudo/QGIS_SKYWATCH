"""
年間撮像上限 in QGIS (tsudo, 2026-11-11): the day's calculation holds back
what goes past a quota and says what is left under the list; 「年間上限…」
(and the menu's 「年間撮像上限…」) lists each quota's year with its shots, and
marks scenes the system does not count on the shot layer.
"""
import json

import pytest

pytest.importorskip("qgis.core")

from qgis.PyQt.QtWidgets import QDialog  # noqa: E402

from mikage.core.request.daily import QUOTA, TAKEN  # noqa: E402
from mikage.core.request.model import jst_day  # noqa: E402
from mikage.tests import test_qgis_request_shots as shots_test  # noqa: E402
from mikage.tests.fake_esri import FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_plan import T  # noqa: E402
from mikage.ui import quota_dialog  # noqa: E402
from mikage.ui.quota_dialog import QUOTA_COLUMNS, SHOT_COLUMNS  # noqa: E402

plugin = shots_test.plugin
QUOTAS = "https://example.com/arcgis/rest/services/Req/FeatureServer/23"


def _with_quota(plugin, limit=1):
    p, fake, tmp_path = shots_test._setup(plugin)
    fake.add_layer(QUOTAS, FakeLayer({"SENSOR": "esriFieldTypeString", "SENSOR_MODE": "esriFieldTypeString", "ANNUAL_LIMIT": "esriFieldTypeInteger",
                                      "YEAR_START_MONTH": "esriFieldTypeSmallInteger", "INITIAL_USED": "esriFieldTypeInteger",
                                      "COUNT_FROM": "esriFieldTypeDate", "NOTE": "esriFieldTypeString"}, "esriGeometryNone"))
    # OPT 1 has no sensor: its quota is named after it.
    fake.layers[QUOTAS].add({"SENSOR": "OPT 1", "ANNUAL_LIMIT": limit, "YEAR_START_MONTH": 4})
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    config["requestStores"][0]["quotas"] = QUOTAS
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.api.reload_config()
    assert p.api.problems == [], p.api.problems
    return p, fake, tmp_path


def test_the_day_kept_within_the_quota_and_scenes_marked_not_counted(plugin, monkeypatch):
    p, fake, tmp_path = _with_quota(plugin)
    daily = shots_test._calculate(p, tmp_path)
    # 北 and 南 would be one shot of 2 scenes: only 1 is left, so 北 goes alone and 南 is held back.
    got = {c.request.name: (c.result, c.reason) for c in daily.plan.candidates}
    assert got["北"][0] == TAKEN and got["南"] == (got["南"][0], QUOTA) and got["南"][0] != TAKEN
    assert not daily.quota_text.isHidden()
    assert daily.quota_text.text().startswith("年間上限 OPT 1（全モード）: 年間 1 枚のうち 消費 0・今回 1・残り 0（")
    assert daily.quota_text.text().endswith("※残りわずか")
    daily.write(ask=False)
    assert wait_for(lambda: daily.task is None and len(fake.layers[shots_test.SHOTS].rows) == 1), daily.status.text()
    shot = fake.layers[shots_test.SHOTS].rows[0]["attributes"]
    assert (shot["SATELLITE"], shot["SCENES"], shot["CAPTURED"]) == ("OPT 1", 1, "未判定")

    # The menu's 「年間撮像上限…」 opens the dock's window, on the day chosen there.
    next(a for a in p.actions if a.objectName() == "SatObsQuotas").trigger()
    window = daily.quota_window
    assert window is not None
    assert wait_for(lambda: window.task is None and window.usages), window.status.text()
    assert window.quota_model.rowCount() == 1
    cell = lambda col: window.quota_model.item(0, QUOTA_COLUMNS.index(col)).text()  # noqa: E731
    assert (cell("年間上限"), cell("見込み"), cell("残り")) == ("1", "1", "0")
    assert window.shot_model.rowCount() == 1
    assert window.shot_model.item(0, SHOT_COLUMNS.index("状態")).text() == "見込み"
    assert window.shot_model.item(0, SHOT_COLUMNS.index("対象日")).text() == jst_day(T).isoformat()

    # 数えない…: the scenes and why, on the shot layer (never on the image catalog).
    window.shot_table.selectRow(0)
    assert window.uncount_button.isEnabled() and not window.count_button.isEnabled()
    monkeypatch.setattr(quota_dialog.UncountedDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window.uncount()
    assert wait_for(lambda: window.task is None and "数えないに" in window.status.text()), window.status.text()
    shot = fake.layers[shots_test.SHOTS].rows[0]["attributes"]
    assert (shot["UNCOUNTED_SCENES"], shot["UNCOUNTED_REASON"]) == (1, "ノイズ")
    assert (cell("見込み"), cell("残り")) == ("", "1")
    window.shot_table.selectRow(0)
    window.count_again()
    assert wait_for(lambda: window.task is None and "数えるに戻した" in window.status.text()), window.status.text()
    shot = fake.layers[shots_test.SHOTS].rows[0]["attributes"]
    assert shot["UNCOUNTED_SCENES"] is None and shot["UNCOUNTED_REASON"] is None
    window.close()


def test_enough_left_and_the_quota_table_unreadable(plugin):
    p, fake, tmp_path = _with_quota(plugin, limit=10)
    daily = shots_test._calculate(p, tmp_path)
    assert all(c.result == TAKEN for c in daily.plan.candidates)
    assert "今回 2・残り 8" in daily.quota_text.text()
    # The table cannot be read: the day is calculated without the quotas, said so.
    del fake.layers[QUOTAS]
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    assert all(c.result == TAKEN for c in daily.plan.candidates)
    assert daily.quota_text.isHidden() and "年間撮像上限" in daily.status.text()
