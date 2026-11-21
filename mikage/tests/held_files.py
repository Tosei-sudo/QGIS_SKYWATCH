"""
Run by test_qgis_ai.test_after_the_ai_tools_ran_nothing_in_the_plugin_folder_is_held
in a Python of its own (the Windows zip's packages only, none installed):

    python -m mikage.tests.held_files <plugin folder> <copies folder> <detect.onnx> <GenAI model folder> <external-data model>

loads ONNX Runtime, ONNX Runtime GenAI and the C++ sgp4 from
<plugin folder>/ext/win64 as the plugin does on Windows, runs the models,
and prints (JSON) which files of the plugin's folder the process holds then,
and after the models are let go (ai_runtime.forget_models, as when the
plugin unloads). Held = mapped (a .so is a loaded DLL on Windows; a model's
data) or open: files Windows would not let QGIS overwrite or remove.
"""
import gc
import json
import os
import sys


def held(under: str) -> list[str]:
    out = set()
    with open("/proc/self/maps") as f:
        for line in f:
            parts = line.split(None, 5)
            if len(parts) == 6 and parts[5].strip().startswith(under + os.sep):
                out.add(os.path.relpath(parts[5].strip(), under).replace(os.sep, "/"))
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(under + os.sep):
            out.add(os.path.relpath(target, under).replace(os.sep, "/"))
    return sorted(out)


def main(plugin: str, cache: str, detect: str, tiny: str, big: str) -> dict:
    sys.path[:] = [p for p in sys.path
                   if not any(os.path.isdir(os.path.join(p, n)) for n in ("onnxruntime", "onnxruntime_genai", "sgp4"))]
    import numpy as np

    from mikage.core import win64
    from mikage.qgis_io import ai_runtime, text_assist_io

    vendored = os.path.join(plugin, "ext", "win64")
    ort = ai_runtime.load_ort(vendored, "Windows")
    og = text_assist_io.load_genai(vendored, "Windows")
    win64.add_package("sgp4", vendored)
    import sgp4.api

    loaded_from = {m.__name__: m.__file__ for m in (ort, og, sgp4.api)}
    ai_runtime.model_from(detect).run({"images": np.zeros((1, 3, 64, 64), np.float32)})
    writer = text_assist_io.GenAiWriter(tiny, threads=1)
    reply = writer.write([{"role": "user", "content": "x"}], 16).strip()
    y = ai_runtime.model_from(big).run({"x": np.ones(1024, np.float32)})["y"]
    out = {"from": loaded_from, "accelerated": bool(sgp4.api.accelerated), "reply": reply, "y": y[:3].tolist(),
           "loaded": held(plugin), "copies": held(cache)}
    ai_runtime.forget_models()
    gc.collect()
    out["let go"] = held(plugin)
    return out


if __name__ == "__main__":
    print(json.dumps(main(*sys.argv[1:6])))
