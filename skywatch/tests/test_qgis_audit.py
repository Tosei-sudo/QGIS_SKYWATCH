"""監査ログ in QGIS (ui/audit_status.py and the places that record): started with the plugin, sent, checked."""
import json
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsSettings, QgsVectorLayer  # noqa: E402

from skywatch.core import audit  # noqa: E402
from skywatch.core.audit import AuditLog, AuditSettings  # noqa: E402
from skywatch.qgis_io import settings  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402


def records(folder):
    out = []
    for _where, line in audit.read_folder(str(folder)):
        rec = audit.parse_line(line)
        if rec:
            out.append(rec)
    return out


@pytest.fixture
def plugin(tmp_path):
    QgsSettings().remove("skywatch")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"auditLog": {"folder": str(tmp_path / "share" / "{computer}"), "localFolder": str(tmp_path / "here")}}),
                      encoding="utf-8")
    settings.set_config_source(str(config))
    settings.set_startup_layers_on(False)
    from skywatch import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p, iface, tmp_path
    if p.audit is not None:
        p.unload()
    QgsSettings().remove("skywatch")


def test_the_plugin_records_its_start_the_settings_and_its_end_and_sends_them(plugin):
    p, iface, tmp_path = plugin
    log = p.audit.log
    assert audit.current() is log and log.settings.local_folder == str(tmp_path / "here")
    assert wait_for(lambda: len(records(tmp_path / "here")) >= 2)
    start, config = records(tmp_path / "here")[:2]
    assert (start["kind"], start["action"]) == ("session", "start") and start["detail"]["plugin"]
    assert (config["kind"], config["action"], config["result"]) == ("config", "read", "ok")
    assert config["target"] == str(tmp_path / "config.json") and len(config["detail"]["sha256"]) == 64
    # 「監査ログ…」: what is recorded where; 「今すぐ送信」 sends without waiting for the interval.
    dialog = p.open_audit_log()
    text = dialog.summary.text()
    assert str(tmp_path / "here") in text and "最後の送信: まだ" in text
    dialog.send()
    share = tmp_path / "share" / log.computer()
    assert wait_for(lambda: share.is_dir() and len(records(share)) >= 2)
    assert wait_for(lambda: "最後の送信: 20" in dialog.summary.text(), 3000)
    dialog.check_folder()
    assert wait_for(lambda: "問題 0 件" in dialog.report.toPlainText())
    p.unload()
    assert audit.current() is None
    assert wait_for(lambda: any(r["kind"] == "session" and r["action"] == "end" for r in records(share)), 3000)
    text, fine = audit.check_text(str(share))
    assert fine, text


def test_a_folder_that_cannot_be_written_is_told_once(plugin, monkeypatch):
    p, iface, tmp_path = plugin
    keeper = p.audit
    told = []
    monkeypatch.setattr(iface.bar, "pushMessage", lambda *args: told.append(args))
    keeper.log.status.send_error = "アクセスが拒否されました"
    keeper.check()
    keeper.check()
    assert len([m for m in told if "送信先フォルダに送れません" in str(m)]) == 1
    keeper.log.status.send_error = ""
    keeper.check()
    keeper.log.status.send_error = "ネットワーク パスが見つかりません"
    keeper.check()
    assert len([m for m in told if "送信先フォルダに送れません" in str(m)]) == 2


def test_exports_opened_images_and_qgis_edits_are_recorded(tmp_path):
    from types import SimpleNamespace

    from skywatch.qgis_io.catalog_io import add_record_layers
    from skywatch.qgis_io.edit_stamp_layers import EditStampLayers
    from skywatch.tests.test_qgis_observations import geotiff
    from skywatch.ui.docks import save_text

    log = AuditLog(str(tmp_path / "here"), AuditSettings(), session="t", computer="PC", user="u")
    audit.set_log(log)
    assert save_text(str(tmp_path / "out.csv"), "a,b\n", what="observations-csv", count=1) is None
    assert save_text(str(tmp_path / "missing" / "x.csv"), "a") is not None  # not written: not recorded
    layer = QgsRasterLayer(geotiff(tmp_path / "img.tif", "2026-10-01T01:00:00Z"), "img")
    add_record_layers([layer], SimpleNamespace(id="IMG-1"), QgsProject.instance())
    # QGIS's own edit of a layer (an Esri one in use; a memory layer here): counted once the save went through.
    edits = QgsVectorLayer("Point?crs=EPSG:4326&field=NAME:string", "points", "memory")
    watcher = EditStampLayers()
    edits.startEditing()
    f = QgsFeature(edits.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(139, 35)))
    edits.addFeature(f)
    watcher._before_commit(edits)
    assert edits.commitChanges()
    watcher._after_commit(edits)
    log.write_queued()
    export, opened, saved = records(tmp_path / "here")
    assert (export["kind"], export["action"], export["count"], export["detail"]["bytes"]) == ("export", "observations-csv", 1, 4)
    assert (opened["kind"], opened["target"], opened["detail"]["files"]) == ("open", "IMG-1", [str(tmp_path / "img.tif")])
    assert (saved["kind"], saved["action"], saved["detail"]["added"], saved["detail"]["by"]) == ("edit", "save", 1, "qgis")
    QgsProject.instance().removeAllMapLayers()


def test_work_kept_offline_and_thrown_away_is_recorded(tmp_path):
    from skywatch.core.offline import OfflineWork

    log = AuditLog(str(tmp_path / "here"), AuditSettings(), session="t", computer="PC", user="u")
    audit.set_log(log)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    work.keep("observation", "共有", "OBS-1", "add", {"attributes": {}}, title="船舶")
    work.discard()
    log.write_queued()
    [kept] = records(tmp_path / "here")
    assert (kept["kind"], kept["action"], kept["target"], kept["ids"], kept["detail"]["kind"]) == ("offline", "keep", "共有", ["OBS-1"], "observation")
    work.close()
