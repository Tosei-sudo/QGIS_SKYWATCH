"""
The features' doors (core/<機能>/api.py, core/door.py): each name a door gives is there when taken, imported from its
module only then, so the plugin's start reads only what it uses.
"""
import importlib
import subprocess
import sys

import pytest

from mikage.core.door import door_names

DOORS = ["ai", "catalog", "enhance", "obs", "plan", "request"]


@pytest.mark.parametrize("feature", DOORS)
def test_every_name_of_the_door_comes(feature):
    door = importlib.import_module(f"mikage.core.{feature}.api")
    names = door_names(open(door.__file__, encoding="utf-8").read())
    assert names, "the door lists its names in `if TYPE_CHECKING:`"
    # Each name it lists is in its __all__, and every name of __all__ comes.
    assert set(names) <= set(door.__all__), sorted(set(names) - set(door.__all__))
    for name in door.__all__:
        assert getattr(door, name) is not None, name
    assert set(names) <= set(dir(door))
    with pytest.raises(AttributeError):
        door.no_such_name  # noqa: B018


def test_a_name_imports_only_its_module():
    """撮像依頼's door taken for one name does not read the others (follow_job's urllib, simulate…)."""
    code = ("import sys\n"
            "from mikage.core.request.api import ImagingRequest\n"
            "print(sorted(m for m in sys.modules if m.startswith('mikage.core.request.')))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "mikage.core.request.model" in out
    assert "mikage.core.request.follow_job" not in out and "mikage.core.request.simulate" not in out
