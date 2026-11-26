"""
MIKAGE 練習版 as it is installed: the zip of `scripts/build_zip.py
--training-only` unpacked into a plugins folder and started by its own
package name (mikage_training) in a QGIS of its own (a subprocess), as QGIS
does. It is that plugin by its marker, keeps its settings under its own root
and practises once QGIS has started.
"""
import json
import os
import subprocess
import sys
import zipfile

import pytest

pytest.importorskip("qgis.core")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT = r'''
import json, os, sys
plugins, root, folder = sys.argv[1:4]
sys.path.insert(0, plugins)
sys.path.insert(1, root)
from qgis.testing import start_app
APP = start_app()
from qgis.core import QgsSettings
QgsSettings().remove("mikage_training")
import mikage_training
from mikage_training.core import edition
from mikage_training.qgis_io import qsettings, training_io
from mikage_training.ui import first_setup, offline_status, shape_receiver
from mikage_training.qgis_io import preload, stall_watch
# As the tests' conftest does for MIKAGE: no page popping up, no listening port, no reads ahead, no stall notes.
first_setup.ENABLED = False
shape_receiver.ShapeReceiver.LISTEN = False
preload.Preloader.DELAY_MS = 24 * 3600 * 1000
stall_watch.StallWatch.ENABLED = False
offline_status.ask_upload = lambda parent, work, back: "later"
training_io.folder = lambda: folder
from mikage.tests.qgis_fakes import FakeIface, wait_for
iface = FakeIface()
p = mikage_training.classFactory(iface)
p.initGui()
started = wait_for(lambda: training_io.active() is not None, 60000)
session = training_io.active()
p.role_keeper.choose(["OBSERVER"])
out = {
    "package": type(p).__module__,
    "training_only": edition.training_only(),
    "plugin_id": edition.plugin_id(),
    "started": started,
    "source_is_practice": started and p.context.source == session.files.config,
    "roles_key": qsettings.key("roles"),
    "roles_kept": QgsSettings().value("mikage_training/roles", ""),
    "menus": sorted({m for m, _t in iface.menus}),
    "end": p.end_training(),
    "audit": p.audit is not None,
}
p.unload()
QgsSettings().remove("mikage_training")
print("RESULT " + json.dumps(out, ensure_ascii=False))
'''


def _build(out_dir: str) -> str:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import build_zip
    finally:
        sys.path.pop(0)
    return build_zip.main("", out_dir, training_only=True)


def test_the_unpacked_zip_runs_as_mikage_training(tmp_path):
    target = _build(str(tmp_path / "dist"))
    plugins = tmp_path / "plugins"
    with zipfile.ZipFile(target) as z:
        z.extractall(plugins)
    assert sorted(os.listdir(plugins)) == ["mikage_training"]
    script = tmp_path / "run" / "start.py"
    script.parent.mkdir()
    script.write_text(SCRIPT, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, str(script), str(plugins), ROOT, str(tmp_path / "training")],
                          capture_output=True, text=True, timeout=240, env=env, cwd=str(tmp_path))
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, done.stdout[-3000:] + done.stderr[-3000:]
    out = json.loads(lines[-1][len("RESULT "):])
    assert out["package"] == "mikage_training.plugin"
    assert out["training_only"] and out["plugin_id"] == "mikage_training"
    assert out["started"] and out["source_is_practice"]
    assert out["roles_key"] == "mikage_training/roles" and json.loads(out["roles_kept"]) == ["OBSERVER"]
    assert out["menus"] == ["MIKAGE 練習版"]
    assert out["end"] is False and out["audit"] is False
