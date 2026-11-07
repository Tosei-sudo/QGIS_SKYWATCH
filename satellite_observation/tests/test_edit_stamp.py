"""core/edit_stamp.py and EsriClient.apply_edits: who and when in every Esri layer written."""
import json
from urllib.parse import parse_qs

from satellite_observation.core import edit_stamp
from satellite_observation.core.config import parse_config
from satellite_observation.core.edit_stamp import DEFAULT_FIELDS, EditTracking, edit_tracking_of, stamp, user_name
from satellite_observation.core.esri import EsriClient, EsriError

URL = "https://gis.example/arcgis/rest/services/X/FeatureServer/0"
TYPES = {"NAME": "esriFieldTypeString", "CREATED_DATE": "esriFieldTypeDate", "created_user": "esriFieldTypeString",
         "UPDATED_DATE": "esriFieldTypeDate", "UPDATED_USER": "esriFieldTypeString"}


def test_user_is_the_environment_variable_not_the_sign_in():
    assert user_name(EditTracking(), {"USERNAME": " tsudo ", "USER": "root"}) == "tsudo"
    assert user_name(EditTracking(user_variable="SATOBS_USER"), {"USERNAME": "a", "SATOBS_USER": "b"}) == "b"
    assert user_name(EditTracking(user="固定"), {"USERNAME": "a"}) == "固定"
    assert user_name(EditTracking(), {}) == ""


def test_adds_get_all_four_and_updates_the_updated_two():
    t = EditTracking()
    [added] = stamp([{"attributes": {"NAME": "a"}, "geometry": {"x": 1}}], TYPES, True, 1000, t, "u1")
    assert added == {"attributes": {"NAME": "a", "CREATED_DATE": 1000, "created_user": "u1", "UPDATED_DATE": 1000, "UPDATED_USER": "u1"},
                     "geometry": {"x": 1}}
    [updated] = stamp([{"attributes": {"OBJECTID": 3, "NAME": "b"}}], TYPES, False, 2000, t, "u2")
    assert updated["attributes"] == {"OBJECTID": 3, "NAME": "b", "UPDATED_DATE": 2000, "UPDATED_USER": "u2"}


def test_layers_without_the_fields_or_switched_off_are_left_alone():
    features = [{"attributes": {"NAME": "a"}}]
    assert stamp(features, {"NAME": "esriFieldTypeString"}, True, 1, EditTracking(), "u") is features
    assert stamp(features, TYPES, True, 1, EditTracking(enabled=False), "u") is features
    # No user known: the times only.
    [f] = stamp(features, TYPES, True, 5, EditTracking(), "")
    assert f["attributes"] == {"NAME": "a", "CREATED_DATE": 5, "UPDATED_DATE": 5}


def test_text_fields_take_iso_times():
    [f] = stamp([{"attributes": {}}], {"CREATED_DATE": "esriFieldTypeString"}, True, 0, EditTracking(), "u")
    assert f["attributes"] == {"CREATED_DATE": "1970-01-01T00:00:00.000Z"}


def test_the_settings_file():
    problems = []
    t = edit_tracking_of({"userVariable": "LOGIN", "user": "", "fields": {"createdAt": "INS_DATE", "updatedBy": None}}, problems)
    assert problems == []
    assert (t.enabled, t.user_variable, t.user) == (True, "LOGIN", "")
    assert dict(t.fields) == {**DEFAULT_FIELDS, "createdAt": "INS_DATE", "updatedBy": ""}
    assert edit_tracking_of(False, problems).enabled is False
    edit_tracking_of({"fields": {"who": "X"}, "userVariable": 3}, problems)
    assert len(problems) == 2
    config, problems = parse_config({"editTracking": {"user": "固定"}})
    assert problems == [] and config.edit_tracking.user == "固定"
    assert parse_config({})[0].edit_tracking == edit_stamp.DEFAULT_EDIT_TRACKING


class Server:
    def __init__(self, fields):
        self.fields = fields
        self.edits = []

    def __call__(self, url, form):
        if url == URL:
            if self.fields is None:
                raise EsriError("no info")
            return json.dumps({"fields": [{"name": n, "type": t} for n, t in self.fields.items()], "objectIdField": "OBJECTID"}).encode()
        assert url == URL + "/applyEdits"
        self.edits.append(form)
        adds = json.loads(form.get("adds", "[]"))
        updates = json.loads(form.get("updates", "[]"))
        return json.dumps({"addResults": [{"success": True, "objectId": i + 1} for i in range(len(adds))],
                           "updateResults": [{"success": True} for _ in updates]}).encode()


def test_apply_edits_stamps_from_the_environment(monkeypatch):
    monkeypatch.setenv("USERNAME", "yamada")
    server = Server(TYPES)
    EsriClient(server).apply_edits(URL, adds=[{"attributes": {"NAME": "a"}}], updates=[{"attributes": {"OBJECTID": 9}}])
    form = server.edits[0]
    [add], [update] = json.loads(form["adds"]), json.loads(form["updates"])
    assert add["attributes"]["created_user"] == "yamada" and add["attributes"]["UPDATED_USER"] == "yamada"
    assert add["attributes"]["CREATED_DATE"] == add["attributes"]["UPDATED_DATE"] > 1.7e12
    assert set(update["attributes"]) == {"OBJECTID", "UPDATED_DATE", "UPDATED_USER"}


def test_apply_edits_follows_the_settings_and_survives_no_layer_information(monkeypatch):
    monkeypatch.setenv("SATOBS_WHO", "suzuki")
    edit_stamp.set_tracking(EditTracking(user_variable="SATOBS_WHO"))
    server = Server(TYPES)
    EsriClient(server).apply_edits(URL, adds=[{"attributes": {}}])
    assert json.loads(server.edits[0]["adds"])[0]["attributes"]["created_user"] == "suzuki"
    edit_stamp.set_tracking(EditTracking(enabled=False))
    EsriClient(server).apply_edits(URL, adds=[{"attributes": {}}])
    assert json.loads(server.edits[1]["adds"])[0]["attributes"] == {}
    edit_stamp.set_tracking(None)
    blind = Server(None)
    EsriClient(blind).apply_edits(URL, adds=[{"attributes": {"NAME": "a"}}])
    assert json.loads(blind.edits[0]["adds"])[0]["attributes"] == {"NAME": "a"}
