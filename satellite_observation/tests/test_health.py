"""「接続と表の点検」 (core/health.py) against a fake service."""
import json

from satellite_observation.core.config import parse_config
from satellite_observation.core.esri import EsriClient
from satellite_observation.core.health import NOTE, OK, PROBLEM, check_all, report_text, targets
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer
from satellite_observation.tests.test_obs import ALL, DATES

OBS = "https://x/arcgis/rest/services/Obs/FeatureServer/0"
WORK = "https://x/arcgis/rest/services/Work/FeatureServer/0"
RULES = "https://x/arcgis/rest/services/Alerts/FeatureServer/0"
LOG = "https://x/arcgis/rest/services/Alerts/FeatureServer/2"
S, D = "esriFieldTypeString", "esriFieldTypeDate"


def config():
    c, problems = parse_config({
        "observationStores": [{"label": "共有", "url": OBS}],
        "assignments": WORK,
        "alertReports": {"rules": RULES, "table": LOG, "formats": [{"name": "既定", "fields": ["kind"]}]},
    })
    return c


def test_the_services_are_found_with_what_is_expected_of_them():
    found = {t.path: t for t in targets(config())}
    assert {"observationStores[0]", "assignments", "alertReports.rules", "alertReports.table"} <= set(found)
    assert found["observationStores[0]"].written and found["observationStores[0]"].label == "共有"
    assert found["observationStores[0]"].fields["id"] == ["OBS_ID"]
    assert found["assignments"].written and not found["alertReports.rules"].written


def test_missing_attributes_a_read_only_layer_and_a_refused_sign_in_are_told():
    fake = FakeEsri()
    # The observation layer without OBS_ID (needed) and CONFIDENCE (one feature's), read only.
    obs = fake.add_layer(OBS, FakeLayer({n: DATES.get(n, S) for n in ALL if n != "OBS_ID"}))
    info = obs.info
    obs.info = lambda: {**info(), "capabilities": "Query"}
    fake.add_layer(WORK, FakeLayer({"IMAGE_ID": S, "IMAGE_NAME": S, "IMAGE_TIME": D, "ASSIGNEE": S, "STATUS": S, "NOTE": S,
                                    "TAKEN_AT": D, "DONE_AT": D}, "", token="right"))
    # The rules layer is not there at all; the log table is.
    fake.add_layer(LOG, FakeLayer({"OBS_ID": S, "REPORT": S, "TEXT": S, "REPORTED_AT": D, "REPORTED_BY": S}, ""))
    results = {r.target.path: r for r in check_all(EsriClient(fake), config(), lambda url, token: token)}
    texts = {p: " / ".join(f.text for f in r.findings) for p, r in results.items()}
    assert results["observationStores[0]"].level == PROBLEM
    assert "属性 OBS_ID がない（役割 id。この表が使えない）" in texts["observationStores[0]"]
    assert "属性 CONFIDENCE がない" in texts["observationStores[0]"]
    assert "編集できない" in texts["observationStores[0]"]
    assert "作成者・更新者の属性 CREATED_DATE" in texts["observationStores[0]"]
    assert results["assignments"].level == PROBLEM and "サインイン" in texts["assignments"]
    assert results["alertReports.rules"].level == PROBLEM and "応答なし" in texts["alertReports.rules"]
    assert results["alertReports.table"].level in (OK, NOTE)
    text = report_text(list(results.values()), "2026-11-08 15:00", "D:/config.json")
    assert text.startswith("接続と表の点検（2026-11-08 15:00）\n設定ファイル: D:/config.json\n")
    # Problems first.
    assert text.index("[問題]") < text.index("alertReports.table")
