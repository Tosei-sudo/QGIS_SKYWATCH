"""
Run by test_qgis_ai.test_after_a_session_with_the_generative_ai_nothing_in_the_plugin_folder_is_held
in a Python of its own, from a copy of the plugin laid out like the Windows
zip (`<plugins>/mikage` with ext/win64: onnxruntime, onnxruntime-genai, the
C++ sgp4 and tzdata; none of them installed):

    python -m mikage.tests.plugin_session_held <GenAI model folder>

A session of QGIS as on a person's PC: the plugin started with settings that
give 文章の手伝い a model, the generative AI turned on, times read in a time
zone (tzdata, as Windows has no time zone database), a note tidied by the
real ONNX Runtime GenAI, then the plugin unloaded, as QGIS does just before
it updates or uninstalls it. Prints (JSON) which files of the plugin's folder
the process holds after the AI ran and after the unload (tests/held_files.py:
mapped or open, as Windows would lock them), where each library came from,
and whether ext/win64 was ever on Python's path.

2026-11-24 (生成 AI を使うとアンインストールにも失敗する): the earlier test
loaded the libraries by hand in its own order and missed that tzdata put
ext/win64 on the path, so the AI's DLLs were loaded from the plugin's
folder. This one runs the plugin's own code in a session's order.
"""
import json
import os
import sys

HIDDEN = ("onnxruntime", "onnxruntime_genai", "sgp4", "tzdata")


def main(model: str) -> dict:
    # Python has none of the zip's libraries of its own (as QGIS on Windows).
    sys.path[:] = [p for p in sys.path if not any(os.path.isdir(os.path.join(p, n)) for n in HIDDEN)]
    from qgis.testing import start_app

    app = start_app()
    from qgis.core import QgsSettings

    from mikage.core import time_zone, win64
    from mikage.features.observations import Observations
    from mikage.qgis_io import settings
    from mikage.tests.feature_harness import small_plugin
    from mikage.tests.held_files import held
    from mikage.tests.qgis_fakes import FakeIface, wait_for

    plugin_folder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vendored = os.path.join(plugin_folder, "ext", "win64")
    seen_on_path = []

    def look() -> None:
        if any(os.path.abspath(p) == os.path.abspath(vendored) for p in sys.path if isinstance(p, str) and p):
            seen_on_path.append(True)

    QgsSettings().remove("mikage")
    config = os.path.join(os.path.dirname(plugin_folder), "config.json")
    with open(config, "w", encoding="utf-8") as f:
        json.dump({"textAssist": {"models": {"small": model}, "tasks": {"tidyNote": "small"}}}, f)
    settings.set_config_source(config)
    iface = FakeIface()
    plugin = small_plugin(iface, Observations)
    plugin.initGui()
    look()
    # Times read in the settings' zone (catalogs, layers, alerts do so).
    tokyo = time_zone.parse_time_zone("Asia/Tokyo")
    look()
    assistant = plugin.context.text_assist
    assistant.set_enabled(True)
    assert wait_for(lambda: assistant.has("tidyNote")), "文章の手伝い not offered"
    result: dict = {}
    assistant.ask("tidyNote", "整える", [{"role": "user", "content": "備考: 岸壁にとまってる"}],
                  lambda text, seconds: result.setdefault("reply", text),
                  lambda why: result.setdefault("failed", why))
    assert wait_for(lambda: result, 60000), "no answer"
    look()
    modules = {n: getattr(sys.modules.get(n), "__file__", None) for n in HIDDEN}
    after_ai = held(plugin_folder)
    plugin.unload()
    app.processEvents()
    look()
    return {"reply": result.get("reply", "").strip(), "failed": result.get("failed"), "zone": getattr(tokyo, "name", None),
            "modules": modules, "after ai": after_ai, "after unload": held(plugin_folder), "on path": bool(seen_on_path),
            "told": [t for t, _ in win64._told.values()]}


if __name__ == "__main__":
    out = main(sys.argv[1])
    sys.stdout.write("\nRESULT " + json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0)  # The tiny GenAI model's teardown at exit is known to abort (test-routine notes): the result is out.
