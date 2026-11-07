"""
tools/config_builder.html (the settings file maker) carries a copy of the
plugin's keys, field roles and choices. This checks the copy is current.

To update it after changing the plugin:

    python3 -m satellite_observation.tests.test_config_builder
"""
import json
import os
import re

HTML = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "config_builder.html")
BLOCK = re.compile(r'(<script type="application/json" id="plugin-constants">\n)(.*?)(\n</script>)', re.S)


def plugin_constants() -> dict:
    """What the tool needs from the plugin, from the plugin's own definitions."""
    from satellite_observation.core import ai_results, basemaps, config, edit_stamp, geocode, layer_templates, moving, overviews, startup_layers
    from satellite_observation.core.ai import model_config, transfer
    from satellite_observation.core.enhance import pipeline
    from satellite_observation.core.obs import model as obs_model, stores as obs_stores
    from satellite_observation.core.plan import modes, names, satellites, store as plan_store
    from satellite_observation.core.request import export, store as request_store, systems

    return {
        "keys": list(config.KEYS),
        "roles": {
            "imageCatalogs": ["id", "acquired", "registered", "systemRegistered", "sensor", "angle", "azimuth", "source"],
            "satelliteCatalogs": list(satellites.SATELLITE_ROLES),
            "sensorCatalogs": list(modes.SENSOR_ROLES),
            "satelliteAliases": list(names.ALIAS_ROLES),
            "planStores": dict(plan_store.DEFAULT_PLAN_FIELDS),
            "requestStores": dict(request_store.DEFAULT_REQUEST_FIELDS),
            "requestHistory": dict(request_store.DEFAULT_HISTORY_FIELDS),
            "observationStores": dict(obs_model.DEFAULT_OBSERVATION_FIELDS),
            "taxonomyTable": dict(obs_stores.DEFAULT_LOCAL_FIELDS),
            "movingFeeds": list(moving.ROLES),
            "detectionOutputs": list(transfer.TRANSFER_ROLES),
            "aiResultSources": list(ai_results.RESULT_ROLES),
            "editTracking": dict(edit_stamp.DEFAULT_FIELDS),
        },
        "exportSources": list(export.SOURCES),
        "geometries": list(export.GEOMETRIES),
        "exportFormats": list(systems.FORMATS),
        "encodings": sorted(set(systems.ENCODINGS.values())),
        "layerTypes": list(startup_layers.TYPES),
        "baseMapTypes": list(basemaps.TYPES),
        "defaultBaseMaps": [b.id for b in basemaps.DEFAULT_BASE_MAPS],
        "geocoderTypes": list(geocode.GEOCODER_TYPES),
        "feedTypes": list(moving.FEED_TYPES),
        "feedKinds": list(moving.KINDS),
        "detectTasks": list(model_config.DETECT_TASKS),
        "detectFormats": list(model_config.DETECT_FORMATS),
        "detectionStatuses": list(transfer.STATUSES),
        "resamplings": list(overviews.RESAMPLINGS),
        "compressions": list(overviews.COMPRESSIONS),
        "overviewLocations": list(overviews.LOCATIONS),
        "overviewScopes": list(overviews.SCOPES),
        "presets": list(pipeline.PRESETS),
        "geometryTypes": list(layer_templates.GEOMETRY_NAMES),
        "fieldTypes": list(layer_templates.DESIGN_TYPES),
    }


def _read():
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def test_constants_match_the_plugin():
    m = BLOCK.search(_read())
    assert m, "plugin-constants の script がない"
    assert json.loads(m.group(2)) == plugin_constants(), (
        "tools/config_builder.html の定数が古い: python3 -m satellite_observation.tests.test_config_builder で更新")


def test_every_key_has_a_page():
    text = _read()
    from satellite_observation.core.config import KEYS

    for key in KEYS:
        assert re.search(r'\{ key: "%s",' % key, text), f"{key} の画面がない"


def write():
    text = _read()
    block = json.dumps(plugin_constants(), ensure_ascii=False, indent=1)
    new = BLOCK.sub(lambda m: m.group(1) + block + m.group(3), text) if BLOCK.search(text) else text
    with open(HTML, "w", encoding="utf-8") as f:
        f.write(new)


if __name__ == "__main__":
    write()
    print("updated", os.path.normpath(HTML))
