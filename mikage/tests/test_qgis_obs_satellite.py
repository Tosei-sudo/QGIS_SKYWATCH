"""
衛星名 in QGIS (tsudo 2026-11-20): an image's satellite found from its layer
(the catalog's sensor, the catalog's attributes kept on it, the file's
metadata, its name, the one typed), and 「衛星不明（入力）」 in the 観測記録 dock.
"""
import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402

from mikage.core.catalog.config import catalog_of  # noqa: E402
from mikage.core.catalog.search import record_of  # noqa: E402
from mikage.core.gpkg import open_local  # noqa: E402
from mikage.core.obs.satellites import SatelliteChoices  # noqa: E402
from mikage.core.obs.stores import LocalAreaStore, LocalObservationStore  # noqa: E402
from mikage.core.plan.names import SatelliteNames  # noqa: E402
from mikage.qgis_io import obs_images, settings  # noqa: E402
from mikage.qgis_io.catalog_io import CATALOG_URL, FEATURE, remember  # noqa: E402
from mikage.qgis_io.obs_images import (  # noqa: E402
    IMAGE_ID, SATELLITE, SATELLITE_TYPED, SatelliteLookup, image_satellite, observed_image, open_images,
)
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402

CATALOG_URL_ = "https://example.com/arcgis/rest/services/Catalog/FeatureServer/0"
CHOICES = SatelliteChoices(catalog=("WV03", "GE01"), satellites=("WorldView-3",),
                           names=SatelliteNames.of([("WorldView-3", "40115", "WV03", "WV3")]))


def ms(day: int) -> int:
    return int(datetime(2026, 10, day, 1, tzinfo=timezone.utc).timestamp() * 1000)


def image(path, driver: str = "GTiff", options=(), **items) -> str:
    """A small image over Tokyo Bay, its acquisition time and `items` in its GDAL metadata."""
    # NITF: room for its corners (else setting them fails, an error where another test turned GDAL's exceptions on).
    options = [*options, "ICORDS=G"] if driver == "NITF" else list(options)
    ds = gdal.GetDriverByName(driver).Create(str(path), 20, 20, 1, gdal.GDT_Byte, options)
    ds.SetGeoTransform([139.6, 0.01, 0, 35.7, 0, -0.01])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    if driver == "GTiff":
        ds.SetMetadataItem("ACQUISITION_START_TIME", "2026-10-01T01:00:00Z")
        for k, v in items.items():
            ds.SetMetadataItem(k, v)
    ds.GetRasterBand(1).Fill(100)
    ds = None
    return str(path)


@pytest.fixture(autouse=True)
def fresh():
    obs_images.forget()
    yield
    obs_images.forget()


def test_where_a_layers_satellite_comes_from(tmp_path):
    lookup = SatelliteLookup(CHOICES)
    # The file's metadata (NITF's image source, as the catalog writes it).
    nitf = QgsRasterLayer(image(tmp_path / "scene.ntf", "NITF", ["ISORCE=WorldView-3"]), "n")
    assert nitf.isValid() and image_satellite(nitf, lookup) == ("WV03", "metadata")
    tif = QgsRasterLayer(image(tmp_path / "s2.tif", SPACECRAFT_NAME="Sentinel-2A"), "t")
    assert image_satellite(tif, lookup) == ("Sentinel-2A", "metadata")
    # A word of its name.
    named = QgsRasterLayer(image(tmp_path / "20241010_WV3_P001.tif"), "w")
    assert image_satellite(named, lookup) == ("WV03", "name")
    # Nothing says: none, then the one typed (kept on the layer).
    plain = QgsRasterLayer(image(tmp_path / "ortho_0001.tif"), "o")
    assert image_satellite(plain, lookup) == ("", "")
    obs_images.set_typed_satellite(plain, " GE01 ")
    assert plain.customProperty(SATELLITE_TYPED) == "GE01" and image_satellite(plain, lookup) == ("GE01", "manual")
    # Only asked for: the other docks (times only) do not read it.
    assert observed_image(tif).satellite == "" and observed_image(tif, lookup).satellite == "Sentinel-2A"
    assert (observed_image(plain, lookup).satellite, observed_image(plain, lookup).satellite_origin) == ("GE01", "manual")


def test_an_image_of_the_catalog(tmp_path):
    problems: list[str] = []
    catalog = catalog_of({"label": "カタログ", "url": CATALOG_URL_, "fields": {"id": "IMAGE_ID", "sensor": "SENSOR", "source": "PATH"}},
                         problems, 0)
    assert catalog is not None and not problems
    record = record_of({"attributes": {"IMAGE_ID": "IMG-1", "SENSOR": "GE01", "PATH": "x"}}, catalog.fields)
    # Opened now: the catalog's sensor on the layer, even over a name or metadata that say another.
    layer = QgsRasterLayer(image(tmp_path / "WV3_scene.tif", SPACECRAFT_NAME="Sentinel-2A"), "IMG-1")
    remember(layer, record, catalog)
    assert layer.customProperty(SATELLITE) == "GE01"
    assert image_satellite(layer, SatelliteLookup(CHOICES)) == ("GE01", "catalog")
    # Opened by an older version (no 衛星名 on the layer): read from the catalog's attributes kept on it.
    old = QgsRasterLayer(image(tmp_path / "old.tif"), "IMG-2")
    old.setCustomProperty(IMAGE_ID, "IMG-2")
    old.setCustomProperty(CATALOG_URL, CATALOG_URL_)
    old.setCustomProperty(FEATURE, json.dumps({"attributes": {"IMAGE_ID": "IMG-2", "SENSOR": "WV03"}}))
    assert image_satellite(old, SatelliteLookup(CHOICES, (catalog,))) == ("WV03", "catalog")
    assert image_satellite(old, SatelliteLookup(CHOICES)) == ("", "")  # the catalog not in the settings any more
    QgsProject.instance().addMapLayers([layer, old])
    try:
        found = {i.id: i.satellite for i in open_images(satellites=SatelliteLookup(CHOICES, (catalog,)))}
        assert found == {"IMG-1": "GE01", "IMG-2": "WV03"}
    finally:
        QgsProject.instance().removeAllMapLayers()


@pytest.fixture
def plugin(tmp_path):
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p, iface, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def test_typed_in_the_dock_for_an_image_that_does_not_say(plugin):
    p, iface, tmp_path = plugin
    project = QgsProject.instance()
    known = QgsRasterLayer(image(tmp_path / "scene.ntf", "NITF", ["ISORCE=WV03", "IDATIM=20261001010000"]), "scene")
    unknown = QgsRasterLayer(image(tmp_path / "ortho_0001.tif"), "ortho")
    project.addMapLayers([known, unknown])
    iface.canvas.setDestinationCrs(known.crs())
    iface.canvas.setExtent(known.extent())
    dock = p.api.observation_dock
    dock.show()
    book = dock.book
    assert {i.name: i.satellite for i in book.images} == {"scene.ntf": "WV03", "ortho_0001.tif": ""}
    assert book.go_to("scene.ntf")
    dock.render()
    assert "（WV03）" in dock.image_select.currentText() and not dock.satellite_row.isVisibleTo(dock)
    assert "衛星名: WV03（メタデータ）" in dock.image_select.toolTip()
    rec = book.add(139.7, 35.6, object="あおば", kind="船舶")
    assert rec.satellite == "WV03"
    # The image that does not say: the row to type it.
    assert book.go_to("ortho_0001.tif")
    dock.render()
    assert dock.satellite_row.isVisibleTo(dock) and "衛星名: 不明" in dock.image_select.toolTip()
    first = book.add(139.71, 35.61, object="", kind="車両")
    area = book.add_area([[139.65, 35.55], [139.75, 35.55], [139.75, 35.65], [139.65, 35.65]], "車両")
    assert first.satellite == "" and area.satellite == ""
    dock.satellite_input.setEditText("GE01")
    dock._set_image_satellite()
    assert unknown.customProperty(SATELLITE_TYPED) == "GE01"
    assert book.current.satellite == "GE01" and book.record(first.id).satellite == "GE01" and book.image_areas()[0].satellite == "GE01"
    assert not dock.satellite_row.isVisibleTo(dock) and "（GE01）" in dock.image_select.currentText()
    # Read again (another session): from the layer, the store.
    dock.refresh_images()
    assert book.current.satellite == "GE01" and book.current.satellite_origin == "manual"
    db = open_local(str(tmp_path / "local.gpkg"))
    try:
        assert {o.object_name: o.satellite for o in LocalObservationStore(db).load()} == {"あおば": "WV03", "車両": "GE01"}
        assert [a.satellite for a in LocalAreaStore(db, "ローカル").load()] == ["GE01"]
    finally:
        db.close()
    # Nothing typed: said, nothing saved.
    dock.satellite_input.setEditText("  ")
    dock._set_image_satellite()
    assert "衛星名を入力" in dock.status.text()
