"""The viewer's test/ai-transfer.test.ts: detection outputs, attributes fitted to the target, what a transfer does."""
from satellite_observation.core.ai.transfer import (
    Plan, TargetField, action_of, detection_output_of, fit_value, missing_fields, signature_of, status_of, target_of, transfer_attributes,
)

OUTPUT = detection_output_of(
    {"label": "DB", "url": "https://gis.example/arcgis/rest/services/D/FeatureServer/0",
     "fields": {"class": "CLASS", "score": "CONF", "status": "STATUS", "area": "AREA", "detectedAt": "DET", "sentAt": "SENT", "length": "LEN"},
     "statusValues": {"corrected": 9}, "constants": {"SRC": "v"}},
    [], 0,
)
FIELDS = {f.name.lower(): f for f in [
    TargetField("CLASS", "esriFieldTypeString", 4),
    TargetField("CONF", "esriFieldTypeDouble"),
    TargetField("STATUS", "esriFieldTypeInteger"),
    TargetField("AREA", "esriFieldTypeDouble"),
    TargetField("DET", "esriFieldTypeDate"),
    TargetField("SENT", "esriFieldTypeDate"),
    TargetField("SRC", "esriFieldTypeString"),
]}


def test_settings_entries_are_checked():
    problems = []
    assert detection_output_of({"url": "https://x/arcgis/rest/services/D/MapServer", "fields": {"class": "C"}}, problems, 0) is None
    assert detection_output_of({"url": "https://x/rest/services/D/FeatureServer/1", "fields": {"colour": "C", "class": "C-1"}}, problems, 1) is None
    assert problems == [
        "detectionOutputs[0] の url は …/FeatureServer/0 のようなレイヤーの URL で指定",
        "detectionOutputs[1] の fields.colour は使用不可の役割",
        "detectionOutputs[1] の fields.class は属性名（英数字と _）で指定",
        "detectionOutputs[1] の fields に書き込む属性なし",
    ]
    assert OUTPUT.status_values == {"ai": "AI", "corrected": 9, "manual": "手動"}
    assert OUTPUT.label == "DB" and OUTPUT.url.endswith("/FeatureServer/0")
    assert missing_fields(OUTPUT, FIELDS) == ["LEN"]


def test_the_models_features_are_told_from_corrected_and_added_ones():
    origin = {"class": "car", "geometry": "1.00,2.00"}
    assert status_of(origin, "car", "1.00,2.00") == "ai"
    assert status_of(origin, "truck", "1.00,2.00") == "corrected"
    assert status_of(origin, "car", "6.00,2.00") == "corrected"
    assert status_of(None, "car", "1.00,2.00") == "manual"


def test_roles_are_mapped_to_the_targets_fields_fitted_to_their_types():
    values = {"class": "truck", "score": 0.876, "status": "corrected", "area": 4000.0, "detectedAt": "2026-10-10T00:00:00Z",
              "sentAt": 1000, "length": 100.0}
    attributes = transfer_attributes(values, OUTPUT, FIELDS)
    assert attributes == {"CLASS": "truc", "CONF": 0.876, "STATUS": 9, "AREA": 4000.0, "DET": 1791590400000, "SENT": 1000, "SRC": "v"}
    assert fit_value("12.6", TargetField("N", "esriFieldTypeSmallInteger")) == 13
    assert fit_value(2.5, TargetField("N", "esriFieldTypeInteger")) == 3
    assert fit_value("", TargetField("N", "esriFieldTypeString")) is None
    assert fit_value("x", TargetField("N", "esriFieldTypeDouble")) is None


def test_new_features_are_added_changed_ones_updated_the_rest_left():
    sig = signature_of({"A": 1, "SENT": 5}, {}, "SENT")
    assert sig == signature_of({"A": 1, "SENT": 9}, {}, "SENT")
    assert action_of(None, sig) == ("add", None)
    assert action_of({"id": 7, "signature": sig}, sig) == ("same", 7)
    assert action_of({"id": 7, "signature": sig}, signature_of({"A": 2}, {}, "SENT")) == ("update", 7)


def test_the_target_layer_and_the_plan_as_said():
    target = target_of("https://x/rest/services/D/FeatureServer/0", {
        "name": "検出結果", "geometryType": "esriGeometryPolygon", "capabilities": "Query,Create",
        "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}, {"name": "CLASS", "type": "esriFieldTypeString", "length": 20}]})
    assert (target.object_id_field, target.can_create, target.can_update) == ("OBJECTID", True, False)
    assert target.fields["class"].length == 20
    assert target_of("https://x/rest/services/D/FeatureServer/0", {"capabilities": "Query,Editing"}).can_update
    assert target_of("https://x/rest/services/D/FeatureServer/0", {}).name == "D"
    plan = Plan(adds=[1, 2, 3], same=1, statuses={"ai": 3, "corrected": 1, "manual": 0})
    assert plan.describe(target, 1) == "検出結果（ポリゴン）へ新規 3 件・更新 0 件・転記済みで変更なし 1 件（AI 3・修正 1）。転記先にない属性 1 個は書き込み対象外"
