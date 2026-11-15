# Given to `qgis --code` by make_shots.py. QGIS runs it with exec() inside a function, where the functions a script
# defines do not see one another, so the scenario is imported as a module instead. A mistake in it ends QGIS
# (written to scenario.log) instead of leaving an error window open on the virtual screen.
import os
import sys
import traceback

sys.path.insert(0, os.environ["SHOTS_CODE"])
try:
    import scenario  # noqa: F401
except Exception:
    with open(os.path.join(os.environ["SHOTS"], "scenario.log"), "a", encoding="utf-8") as f:
        f.write(traceback.format_exc())
    os._exit(1)
