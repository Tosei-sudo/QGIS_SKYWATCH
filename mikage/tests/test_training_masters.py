"""
練習モード's masters read from the production's settings file
(core/training/masters.py) and `training.fromProduction` (settings.py).
"""
from mikage.core.config import load_jsonc, parse_config
from mikage.core.training import masters
from mikage.core.training.sample import training_config
from mikage.core.training.settings import MASTERS, training_settings_of

BASE = "http://127.0.0.1:5555"
REST = f"{BASE}/arcgis/rest/services"
PROD = "https://gis.example.jp/arcgis/rest/services"
OUT = "/practice/出力/撮像計画ファイル"


def practice() -> dict:
    return load_jsonc(training_config(BASE, "/practice/server/files", "/practice/出力"))


def production() -> dict:
    return {
        "satelliteCatalogs": [{"label": "本番衛星", "url": f"{PROD}/Sat/FeatureServer/0", "token": "SECRET",
                               "fields": {"name": "SAT_NAME", "tle1": "TLE_LINE1", "tle2": "TLE_LINE2"}}],
        "requestExports": [{"system": "SYS-A", "format": "csv", "columns": [{"key": "ID", "source": "id"}],
                            "folder": "\\\\share\\systems\\a", "fileName": "a_{date:%Y%m%d}.csv"},
                           {"system": "SYS-J", "format": "json", "schema": "./schemas/j.json"}],
        "observationStores": [{"label": "本番観測記録", "url": f"{PROD}/Obs/FeatureServer/0", "token": "STORE",
                               "taxonomyTable": f"{PROD}/Obs/FeatureServer/1", "activities": ["停泊", "航行"]}],
        "geocoders": [{"type": "features", "label": "本番地名", "url": f"{PROD}/Places/FeatureServer/0", "field": "NAME"}],
        "events": {"layer": f"{PROD}/Events/FeatureServer/0", "links": f"{PROD}/Events/FeatureServer/1", "kinds": ["地震", "訓練"]},
        "glossary": f"{PROD}/Glossary/FeatureServer/0",
        "requestStores": [{"label": "本番撮像依頼", "url": f"{PROD}/Requests/FeatureServer/0"}],
        "alertReports": {"rules": f"{PROD}/Alerts/FeatureServer/0"},
    }


def test_the_masters_are_the_productions_and_what_is_written_stays_on_the_practice_server():
    value, taken = masters.with_production(practice(), production(), list(MASTERS), "/etc/mikage/config.json", OUT)
    assert taken == ["satellites", "taxonomy", "places", "eventKinds", "glossary"]
    # Satellites: the production's (the sample's aliases, sensors and ground stations gone: the production has none).
    assert [c["label"] for c in value["satelliteCatalogs"]] == ["本番衛星"] and value["satelliteCatalogs"][0]["token"] == "SECRET"
    assert not {"sensorCatalogs", "satelliteAliases", "groundStations"} & set(value)
    # Their systems, the files into the practice folder; a path relative to the production's file made whole.
    systems = value["requestExports"]
    assert [s["system"] for s in systems] == ["SYS-A", "SYS-J"] and all(s["folder"] == OUT for s in systems)
    assert systems[1]["schema"].replace("\\", "/") == "/etc/mikage/schemas/j.json"
    # The classes on the practice's store (with the token they were read with); the records on the practice server.
    store = value["observationStores"][0]
    assert store["url"].startswith(REST) and store["label"] == "練習用観測記録"
    assert store["taxonomyTable"] == {"url": f"{PROD}/Obs/FeatureServer/1", "token": "STORE"}
    assert store["activities"] == ["停泊", "航行"]
    assert [g["label"] for g in value["geocoders"]] == ["本番地名"]
    assert value["events"]["kinds"] == ["地震", "訓練"] and value["events"]["layer"].startswith(REST)
    assert value["glossary"] == f"{PROD}/Glossary/FeatureServer/0"
    # Nothing else of the production's.
    assert value["requestStores"][0]["url"].startswith(REST) and value["alertReports"]["rules"].startswith(REST)
    assert value["imageCatalogs"][0]["url"].startswith(REST)
    config, problems = parse_config(value)
    assert problems == [] and [c.label for c in config.satellite_catalogs] == ["本番衛星"]
    # Written to disk without the production's tokens.
    text = masters.config_text(masters.without_tokens(value), "練習用")
    assert "SECRET" not in text and "STORE" not in text and text.startswith("// 練習用\n")
    assert parse_config(load_jsonc(text))[0].satellite_catalogs[0].token is None


def test_only_the_masters_named_and_those_the_production_has():
    value, taken = masters.with_production(practice(), production(), ["places"], "/etc/mikage/config.json", OUT)
    assert taken == ["places"]
    assert value["satelliteCatalogs"][0]["url"].startswith(REST) and "taxonomyTable" in value["observationStores"][0]
    assert value["observationStores"][0]["taxonomyTable"].startswith(REST)
    # A production without them: the sample's.
    value, taken = masters.with_production(practice(), {"imageCatalogs": []}, list(MASTERS), "/etc/mikage/config.json", OUT)
    assert taken == [] and value == practice()
    # Events without kinds in the production: their default kinds while practising too.
    p = production()
    del p["events"]["kinds"]
    sample = practice()
    sample["events"]["kinds"] = ["見本"]
    value, taken = masters.with_production(sample, p, ["eventKinds"], "/etc/mikage/config.json", OUT)
    assert taken == ["eventKinds"] and "kinds" not in value["events"]


def test_from_production_in_the_settings_file():
    problems = []
    assert training_settings_of({}, problems).from_production == tuple(MASTERS)
    assert training_settings_of(True, problems).from_production == tuple(MASTERS)
    assert training_settings_of({"fromProduction": False}, problems).from_production == ()
    assert training_settings_of({"fromProduction": ["glossary", "satellites"]}, problems).from_production == ("satellites", "glossary")
    assert problems == []
    assert training_settings_of({"fromProduction": ["衛星"]}, problems).from_production == ()
    assert training_settings_of({"fromProduction": "all"}, problems).from_production == tuple(MASTERS)
    assert len(problems) == 2 and "fromProduction" in problems[0]
    assert masters.names_text(["satellites", "taxonomy"]) == "衛星・分類体系"
