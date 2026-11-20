"""
The architecture rules (tests/architecture.py, docs/architecture.md): no new
violation, and a fixed one leaves the baseline (it only gets shorter).
"""
import os

import pytest

from mikage.tests import architecture


@pytest.fixture(scope="module")
def measured():
    found = architecture.measure()
    if os.environ.get("MIKAGE_UPDATE_ARCHITECTURE") == "1":
        architecture.write_baseline(found)
    return found


@pytest.fixture(scope="module")
def baseline():
    return architecture.load_baseline()


def _compare(found, allowed, what):
    new = sorted(set(found) - set(allowed))
    gone = sorted(set(allowed) - set(found))
    assert not new, f"{what}: 新しい違反（architecture.py の規則を参照）: {new}"
    assert not gone, f"{what}: 直った違反が台帳に残っている（MIKAGE_UPDATE_ARCHITECTURE=1 で台帳から外す）: {gone}"


def test_layers_point_one_way(measured):
    """R1: core imports no QGIS / Qt and nothing above it; qgis_io not ui; nothing below imports the plugin."""
    assert measured["layers"] == []


def test_features_are_imported_through_their_api(measured, baseline):
    """R2: another feature's inside only through its `api` module."""
    _compare(measured["deep_imports"], baseline["deep_imports"], "R2 機能の窓口")


def test_no_reaching_into_other_objects(measured, baseline):
    """R3: no `other._name` in ui, features and plugin.py (per module, the count may only go down)."""
    found, allowed = measured["private_access"], baseline["private_access"]
    grown = {m: n for m, n in found.items() if n > allowed.get(m, 0)}
    assert not grown, f"R3 他オブジェクトの非公開メンバー参照が増えた: {grown}（台帳 {[allowed.get(m, 0) for m in grown]}）"
    shrunk = {m: n for m, n in allowed.items() if found.get(m, 0) < n}
    assert not shrunk, f"R3 減った分を台帳に反映（MIKAGE_UPDATE_ARCHITECTURE=1）: {shrunk}"


def test_the_kernel_knows_no_feature(measured, baseline):
    """R4: the shared base imports only the shared base."""
    _compare(measured["kernel"], baseline["kernel"], "R4 共通基盤")


def test_no_cycle_spans_two_features(measured, baseline):
    """R5: no import cycle (imports inside functions included) across feature packages."""
    _compare(measured["feature_cycles"], baseline["feature_cycles"], "R5 機能をまたぐ循環")


def test_the_baseline_has_every_rule(baseline):
    assert set(baseline) == {"layers", "deep_imports", "private_access", "kernel", "feature_cycles"}
    assert baseline["layers"] == []


def test_every_relative_import_names_a_module_of_the_plugin():
    """Not in the baseline: a wrong one is a bug (it fails when its function runs)."""
    assert architecture.unresolved_imports(architecture.modules()) == []
