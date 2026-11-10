"""
tools/config_builder.html (the settings file maker) carries a copy of the
plugin's keys, field roles and choices. This checks the copy is current.

To update it after changing the plugin:

    python3 -m skywatch.tests.test_config_builder
"""
import json
import os
import re

HTML = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "config_builder.html")
BLOCK = re.compile(r'(<script type="application/json" id="plugin-constants">\n)(.*?)(\n</script>)', re.S)


def plugin_constants() -> dict:
    """What the tool needs from the plugin, from the plugin's own definitions."""
    from skywatch.core import ai_results, basemaps, config, coords, edit_stamp, events, geocode, layer_templates, moving, overviews, startup_layers
    from skywatch.core.ai import model_config, transfer
    from skywatch.core.catalog import config as catalog_config
    from skywatch.core.enhance import pipeline
    from skywatch.core.obs import export as obs_export
    from skywatch.core.obs import alerts, assignments, handover, model as obs_model, stores as obs_stores
    from skywatch.core.plan import modes, names, satellites
    from skywatch.core.request import export, result_file, store as request_store, systems

    return {
        "keys": list(config.KEYS),
        "roles": {
            "imageCatalogs": list(catalog_config.FIELD_ROLES),
            "satelliteCatalogs": list(satellites.SATELLITE_ROLES),
            "sensorCatalogs": list(modes.SENSOR_ROLES),
            "satelliteAliases": list(names.ALIAS_ROLES),
            "requestStores": dict(request_store.DEFAULT_REQUEST_FIELDS),
            "requestHistory": dict(request_store.DEFAULT_HISTORY_FIELDS),
            "observationStores": dict(obs_model.DEFAULT_OBSERVATION_FIELDS),
            "taxonomyTable": dict(obs_stores.DEFAULT_LOCAL_FIELDS),
            "movingFeeds": list(moving.ROLES),
            "detectionOutputs": list(transfer.TRANSFER_ROLES),
            "aiResultSources": list(ai_results.RESULT_ROLES),
            "editTracking": dict(edit_stamp.DEFAULT_FIELDS),
            "alertRules": dict(alerts.DEFAULT_RULE_FIELDS),
            "alertGeofences": dict(alerts.DEFAULT_FENCE_FIELDS),
            "alertPlaces": dict(alerts.DEFAULT_PLACE_FIELDS),
            "alertCountries": dict(alerts.DEFAULT_COUNTRY_FIELDS),
            "alertLog": dict(alerts.DEFAULT_LOG_FIELDS),
            "assignments": dict(assignments.DEFAULT_ASSIGNMENT_FIELDS),
            "handoverMemos": dict(handover.DEFAULT_HANDOVER_FIELDS),
            "eventLayer": dict(events.DEFAULT_EVENT_FIELDS),
            "eventLinks": dict(events.DEFAULT_LINK_FIELDS),
        },
        "alertSources": list(alerts.SOURCES),
        "alertTimeSources": list(alerts.TIME_SOURCES),
        "alertAngleSources": list(alerts.ANGLE_SOURCES),
        "alertListSources": list(alerts.LIST_SOURCES),
        "anglePresets": dict(coords.ANGLE_PRESETS),
        "anglePlaces": list(coords.ANGLE_PLACES),
        "obsExportSources": list(obs_export.SOURCES),
        "obsExportFileSources": list(obs_export.FILE_SOURCES),
        "obsGeometries": list(obs_export.GEOMETRIES),
        "obsBuiltinFormats": [obs_export.BUILTIN_CSV, obs_export.BUILTIN_JSON],
        "exportSources": list(export.SOURCES),
        "exportListSources": list(export.LIST_SOURCES),
        "geometries": list(export.GEOMETRIES),
        "exportFormats": list(systems.FORMATS),
        "resultTargets": list(result_file.TARGETS),
        "resultKeySources": list(result_file.KEY_SOURCES),
        "resultGeometries": list(result_file.GEOMETRIES),
        "resultEncodings": sorted(set(result_file.ENCODINGS.values())),
        "resultWords": {result_file.ADOPTED: list(result_file.DEFAULT_ADOPTED), result_file.REJECTED: list(result_file.DEFAULT_REJECTED),
                        result_file.WAITING: list(result_file.DEFAULT_WAITING)},
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
        "tools/config_builder.html の定数が古い: python3 -m skywatch.tests.test_config_builder で更新")


def test_every_key_has_a_page():
    text = _read()
    from skywatch.core.config import KEYS

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
