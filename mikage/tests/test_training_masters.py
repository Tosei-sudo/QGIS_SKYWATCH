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
        "signIn": [{"method": "iwa", "portal": "https://gis.example.jp/portal"}],
        "satelliteCatalogs": [{"label": "本番衛星", "url": f"{PROD}/Sat/FeatureServer/0",
                               "fields": {"name": "SAT_NAME", "tle1": "TLE_LINE1", "tle2": "TLE_LINE2"}}],
        "requestExports": [{"system": "SYS-A", "format": "csv", "columns": [{"key": "ID", "source": "id"}],
                            "folder": "\\\\share\\systems\\a", "fileName": "a_{date:%Y%m%d}.csv"},
                           {"system": "SYS-J", "format": "json", "schema": "./schemas/j.json"}],
        "observationStores": [{"label": "本番観測記録", "url": f"{PROD}/Obs/FeatureServer/0",
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
    assert [c["label"] for c in value["satelliteCatalogs"]] == ["本番衛星"]
    # The production's servers signed in to as in production.
    assert value["signIn"] == [{"method": "iwa", "portal": "https://gis.example.jp/portal"}]
    assert not {"sensorCatalogs", "satelliteAliases", "groundStations"} & set(value)
    # Their systems, the files into the practice folder; a path relative to the production's file made whole.
    systems = value["requestExports"]
    assert [s["system"] for s in systems] == ["SYS-A", "SYS-J"] and all(s["folder"] == OUT for s in systems)
    assert systems[1]["schema"].replace("\\", "/") == "/etc/mikage/schemas/j.json"
    # The classes on the practice's store; the records on the practice server.
    store = value["observationStores"][0]
    assert store["url"].startswith(REST) and store["label"] == "練習用観測記録"
    assert store["taxonomyTable"] == f"{PROD}/Obs/FeatureServer/1"
    assert store["activities"] == ["停泊", "航行"]
    assert [g["label"] for g in value["geocoders"]] == ["本番地名"]
    assert value["events"]["kinds"] == ["地震", "訓練"] and value["events"]["layer"].startswith(REST)
    assert value["glossary"] == f"{PROD}/Glossary/FeatureServer/0"
    # Nothing else of the production's.
    assert value["requestStores"][0]["url"].startswith(REST) and value["alertReports"]["rules"].startswith(REST)
    assert value["imageCatalogs"][0]["url"].startswith(REST)
    config, problems = parse_config(value)
    assert problems == [] and [c.label for c in config.satellite_catalogs] == ["本番衛星"]
    assert [r.portal for r in config.sign_in] == ["https://gis.example.jp/portal"]
    text = masters.config_text(value, "練習用")
    assert text.startswith("// 練習用\n") and parse_config(load_jsonc(text))[0].sign_in == config.sign_in


def test_only_the_masters_named_and_those_the_production_has():
    value, taken = masters.with_production(practice(), production(), ["places"], "/etc/mikage/config.json", OUT)
    assert taken == ["places"]
    assert value["satelliteCatalogs"][0]["url"].startswith(REST) and "taxonomyTable" in value["observationStores"][0]
    # The sample's classification and its table of known individuals, on the practice server.
    table = value["observationStores"][0]["taxonomyTable"]
    assert table["url"].startswith(REST) and table["members"].startswith(REST)
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


def test_with_the_organisations_exercises_the_productions_path_mappings_too():
    """判読の練習's organisation's exercises may open the production catalogs' images: their files found as the production finds them."""
    mappings = [{"from": "\\\\nas\\images", "to": "Z:\\images"}]
    p = {**production(), "pathMappings": mappings, "training": {"exercises": {"exercises": f"{PROD}/T/FeatureServer/0", "answers": "x"}}}
    value, taken = masters.with_production(practice(), p, [], "/etc/mikage/config.json", OUT)
    assert taken == [] and value["pathMappings"][-1:] == mappings
    assert masters.exercise_images(p) and not masters.exercise_images(production())
    # Without them, the practice's own only.
    value, _ = masters.with_production(practice(), {**production(), "pathMappings": mappings}, [], "/etc/mikage/config.json", OUT)
    assert mappings[0] not in (value.get("pathMappings") or [])
    # The production's value with its relative paths made whole (the production catalogs read from there).
    whole = masters.made_whole({"imageCatalogs": [{"url": "./catalog.json"}]}, "/etc/mikage/config.json")
    assert whole["imageCatalogs"][0]["url"] != "./catalog.json"


FIELDS = {"id": "IMG_ID", "acquired": "ACQ", "source": "PATH"}


def catalogs_production() -> dict:
    return {
        "imageCatalogs": [
            {"label": "本番だけ", "url": f"{PROD}/A/FeatureServer/0", "fields": FIELDS},
            {"label": "練習用", "url": f"{PROD}/T/FeatureServer/0", "fields": FIELDS, "mode": "training",
             "thumbnail": "./ql/{id}.jpg"},
            {"label": "両方", "url": f"{PROD}/B/FeatureServer/0", "fields": FIELDS, "mode": "both"},
        ],
        "pathMappings": [{"prefix": "\\\\nas\\images", "label": "NAS"}],
        "signIn": [{"method": "iwa", "portal": "https://gis.example.jp/portal"}],
    }


def test_a_catalogs_mode_says_where_it_is_shown():
    """imageCatalogs[].mode (tsudo 2026-11-23): production (the default), training, both."""
    config, problems = parse_config(catalogs_production())
    assert problems == []
    # In production: the production's and both; the practice-only ones aside (判読練習の指導者's).
    assert [c.label for c in config.image_catalogs] == ["本番だけ", "両方"]
    assert [c.label for c in config.practice_catalogs] == ["練習用"]
    assert [c.mode for c in config.image_catalogs] == ["production", "both"]
    value = catalogs_production()
    value["imageCatalogs"][0]["mode"] = "practice"
    config, problems = parse_config(value)
    assert len(problems) == 1 and "mode" in problems[0] and "training" in problems[0]
    assert [c.label for c in config.image_catalogs] == ["本番だけ", "両方"]  # (an unknown mode: production)


def test_the_productions_catalogs_shown_while_practising_come_after_the_practices():
    config, _ = parse_config(practice())
    sample = [c.label for c in config.image_catalogs]
    taken = masters.with_practice_catalogs(config, catalogs_production(), "/etc/mikage/config.json")
    assert taken == ["練習用", "両方"]
    assert [c.label for c in config.image_catalogs] == [*sample, "練習用", "両方"] and config.practice_catalogs == []
    shown = {c.label: c for c in config.image_catalogs}
    # With the production's sign-in (signIn: added to the practice's) ...
    assert shown["練習用"].url == f"{PROD}/T/FeatureServer/0" and [r.portal for r in config.sign_in] == ["https://gis.example.jp/portal"]
    # ... a path relative to the production's settings file made whole ...
    assert shown["練習用"].thumbnail is not None
    # ... and their files where the production finds them (after the practice's own mappings).
    assert config.path_mappings[-1].prefix == "\\\\nas\\images"
    assert masters.read_text(["satellites"], catalogs_production()) == "衛星・画像カタログ 2 件"
    assert masters.read_text([], {"imageCatalogs": []}) == ""


def test_a_practice_settings_files_catalogs_are_all_shown_and_the_same_catalog_only_once():
    """The organisation's practice settings file: its `mode` is not looked at; a production catalog it has too, once."""
    own = {"imageCatalogs": [
        {"label": "組織の練習", "url": f"{PROD}/T/FeatureServer/0", "fields": FIELDS},
        {"label": "書いてある mode", "url": f"{PROD}/X/FeatureServer/0", "fields": FIELDS, "mode": "training"},
    ]}
    config, _ = parse_config(own)
    taken = masters.with_practice_catalogs(config, catalogs_production(), "/etc/mikage/config.json")
    assert taken == ["両方"]
    assert [c.label for c in config.image_catalogs] == ["組織の練習", "書いてある mode", "両方"]
    # A production without any for practising: the practice's own, all shown.
    config, _ = parse_config(own)
    assert masters.with_practice_catalogs(config, {"imageCatalogs": catalogs_production()["imageCatalogs"][:1]}, "") == []
    assert [c.label for c in config.image_catalogs] == ["組織の練習", "書いてある mode"] and config.path_mappings == []
    config, _ = parse_config(own)
    assert masters.with_practice_catalogs(config, None, "") == [] and len(config.image_catalogs) == 2
