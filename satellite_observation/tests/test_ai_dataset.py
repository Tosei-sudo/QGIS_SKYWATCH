"""
Training datasets of images outside the catalog (core.ai.train.dataset):
annotation files of the common formats read as boxes, images paired with
their annotations by name, the dataset file written and read back, added to.
"""
import json
import os

import pytest

from satellite_observation.core.ai.train.dataset import (
    Dataset, DatasetError, Group, Item, Reader, add_group, guess_format, load_dataset, make_group, missing_files, names_from_yaml, pair,
    parse_class_map, read_coco, read_dota, read_voc, read_yolo, save_dataset,
)


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return str(path)


def test_yolo_rows_are_boxes_in_fractions():
    boxes = read_yolo("0 0.5 0.25 0.2 0.1\n1 0.1 0.1 0.3 0.1 0.3 0.3 0.1 0.3\n\n7 0.5 0.5 0.1 0.1\nbad row x y z\n", ["ship", "plane"])
    assert [b.name for b in boxes] == ["ship", "plane", "7"]
    ship = boxes[0].in_pixels(200, 100)
    assert (ship.x1, ship.y1, ship.x2, ship.y2) == pytest.approx((80, 20, 120, 30))
    # OBB: the box around the corners.
    plane = boxes[1].in_pixels(100, 100)
    assert (plane.x1, plane.y1, plane.x2, plane.y2) == pytest.approx((10, 10, 30, 30))


def test_dota_rows_are_the_boxes_around_them():
    text = "imagesource:GoogleEarth\ngsd:0.5\n10 20 40 10 50 40 20 50 large-vehicle 0\n1 2 3\n"
    [b] = read_dota(text)
    assert (b.name, b.x1, b.y1, b.x2, b.y2) == ("large-vehicle", 10, 10, 50, 50)


def test_voc_boxes_count_pixels_from_one():
    xml = """<annotation><size><width>100</width><height>80</height></size>
      <object><name>ship</name><bndbox><xmin>11</xmin><ymin>21</ymin><xmax>30</xmax><ymax>40</ymax></bndbox></object>
      <object><name>car</name><robndbox><cx>50</cx><cy>50</cy><w>20</w><h>10</h><angle>0</angle></robndbox></object>
    </annotation>"""
    ship, car = read_voc(xml)
    assert (ship.name, ship.x1, ship.y1, ship.x2, ship.y2) == ("ship", 10, 20, 30, 40)
    assert (car.x1, car.y1, car.x2, car.y2) == pytest.approx((39, 44, 60, 55))
    with pytest.raises(DatasetError):
        read_voc("<annotation>")


def test_coco_objects_by_file_name():
    coco = read_coco(json.dumps({
        "images": [{"id": 1, "file_name": "a/one.png"}, {"id": 2, "file_name": "two.png"}],
        "categories": [{"id": 5, "name": "ship"}],
        "annotations": [{"image_id": 1, "category_id": 5, "bbox": [10, 20, 30, 40]}, {"image_id": 9, "category_id": 5, "bbox": [0, 0, 1, 1]}],
    }))
    [b] = coco.of("a/one.png")
    assert (b.name, b.x1, b.y1, b.x2, b.y2) == ("ship", 10, 20, 40, 60)
    assert coco.of("ONE.png") and coco.of("two.png") == []


def test_yolo_names_from_data_yaml(tmp_path):
    assert names_from_yaml(write(tmp_path / "a.yaml", "path: x\nnames: [ship, 'plane']\n")) == ["ship", "plane"]
    assert names_from_yaml(write(tmp_path / "b.yaml", "names:\n  0: ship\n  2: car  # note\nnc: 3\n")) == ["ship", "1", "car"]
    assert names_from_yaml(write(tmp_path / "c.yaml", "names:\n  - ship\n  - \"plane\"\n")) == ["ship", "plane"]


def test_formats_guessed_from_the_files(tmp_path):
    assert guess_format(write(tmp_path / "a.txt", "0 0.5 0.5 0.1 0.1\n")) == "yolo"
    assert guess_format(write(tmp_path / "b.txt", "1 2 3 4 5 6 7 8 ship 0\n")) == "dota"
    assert guess_format(write(tmp_path / "c.xml", "<annotation/>")) == "voc"
    assert guess_format(write(tmp_path / "d.json", '{"images": [], "annotations": []}')) == "coco"
    assert guess_format(write(tmp_path / "e.geojson", '{"type": "FeatureCollection"}')) == "vector"


def test_images_paired_by_path_then_by_name(tmp_path):
    images = [str(tmp_path / p) for p in ("images/train/a.png", "images/val/a.png", "images/val/b.png", "images/val/c.png")]
    labels = [str(tmp_path / p) for p in ("labels/train/a.txt", "labels/val/a.txt", "labels/b.txt")]
    items, alone = pair(images, str(tmp_path / "images"), labels, str(tmp_path / "labels"))
    assert [(os.path.relpath(i.image, tmp_path), os.path.relpath(i.labels, tmp_path)) for i in items] == [
        ("images/train/a.png", "labels/train/a.txt"), ("images/val/a.png", "labels/val/a.txt"), ("images/val/b.png", "labels/b.txt")]
    assert alone == [str(tmp_path / "images/val/c.png")]


def yolo_folders(root):
    for name in ("a", "b", "c"):
        write(root / "images" / f"{name}.png")
    write(root / "labels" / "a.txt", "0 0.5 0.5 0.1 0.1\n1 0.2 0.2 0.1 0.1\n")
    write(root / "labels" / "b.txt", "")
    write(root / "labels" / "z.txt", "0 0.5 0.5 0.1 0.1\n")
    write(root / "data.yaml", "names: [ship, plane]\n")


def test_a_yolo_group_from_folders(tmp_path):
    yolo_folders(tmp_path)
    found = make_group(str(tmp_path / "images"), str(tmp_path / "labels"), class_map={"plane": "航空機"})
    g = found.group
    assert g.format == "yolo" and g.names == ["ship", "plane"] and g.class_map == {"plane": "航空機"}
    assert [os.path.basename(i.image) for i in g.items] == ["a.png", "b.png"]
    assert [os.path.basename(p) for p in found.alone] == ["c.png"] and found.unused == 1
    assert all(os.path.isabs(i.image) and os.path.isabs(i.labels) for i in g.items)
    boxes = Reader().boxes(g, g.items[0])
    assert [(b.name, g.kind_of(b.name)) for b in boxes] == [("ship", "ship"), ("plane", "航空機")]
    # Images without annotations kept as background: nothing on them.
    kept = make_group(str(tmp_path / "images"), str(tmp_path / "labels"), background=True)
    assert [os.path.basename(i.image) for i in kept.group.items] == ["a.png", "b.png", "c.png"] and not kept.alone
    assert Reader().boxes(kept.group, kept.group.items[2]) == []


def test_a_coco_group_from_one_file(tmp_path):
    write(tmp_path / "img" / "x" / "one.png")
    write(tmp_path / "img" / "two.png")
    write(tmp_path / "img" / "three.png")
    coco = write(tmp_path / "ann" / "coco.json", json.dumps({
        "images": [{"id": 1, "file_name": "x/one.png"}, {"id": 2, "file_name": "two.png"}, {"id": 3, "file_name": "gone.png"}],
        "categories": [{"id": 1, "name": "ship"}], "annotations": [{"image_id": 1, "category_id": 1, "bbox": [1, 2, 3, 4]}]}))
    found = make_group(str(tmp_path / "img"), coco)
    g = found.group
    assert g.format == "coco" and [(os.path.basename(i.image), i.key) for i in g.items] == [("two.png", "two.png"), ("one.png", "x/one.png")]
    assert found.unused == 1 and [os.path.basename(p) for p in found.alone] == ["three.png"]
    one = next(i for i in g.items if i.key == "x/one.png")
    assert [b.name for b in Reader().boxes(g, one)] == ["ship"]


def test_a_vector_file_for_every_image(tmp_path):
    write(tmp_path / "img" / "a.tif")
    write(tmp_path / "img" / "b.tif")
    labels = write(tmp_path / "ships.gpkg")
    g = make_group(str(tmp_path / "img"), labels, "vector", class_field="kind").group
    assert g.format == "vector" and g.class_field == "kind" and [i.labels for i in g.items] == [labels, labels]


def test_refusals(tmp_path):
    with pytest.raises(DatasetError, match="画像のフォルダー"):
        make_group(str(tmp_path / "none"), "")
    write(tmp_path / "img" / "a.png")
    with pytest.raises(DatasetError, match="注釈ファイルなし"):
        make_group(str(tmp_path / "img"), "")
    with pytest.raises(DatasetError, match="1 ファイル"):
        make_group(str(tmp_path / "img"), write(tmp_path / "a.txt", "0 0.5 0.5 0.1 0.1"))
    with pytest.raises(DatasetError, match="名前=種別"):
        parse_class_map("ship=船舶, plane")
    assert parse_class_map("ship=船舶 > 支援船、 plane → 航空機\n0: 車両") == {"ship": "船舶 > 支援船", "plane": "航空機", "0": "車両"}


def test_written_read_back_and_added_to(tmp_path):
    yolo_folders(tmp_path)
    path = str(tmp_path / "set.json")
    d = Dataset("港湾")
    assert add_group(d, make_group(str(tmp_path / "images"), str(tmp_path / "labels")).group) == 0
    save_dataset(d, path)
    back = load_dataset(path)
    assert back.name == "港湾" and back.created and back.to_json()["groups"] == d.to_json()["groups"]
    # Again with background images: only the one not in it yet is added.
    again = make_group(str(tmp_path / "images"), str(tmp_path / "labels"), background=True).group
    assert add_group(back, again) == 2 and [os.path.basename(i.image) for i in back.groups[1].items] == ["c.png"]
    assert missing_files(back) == []
    os.remove(tmp_path / "labels" / "a.txt")
    assert missing_files(back) == [str(tmp_path / "labels" / "a.txt")]


def test_not_a_dataset(tmp_path):
    with pytest.raises(DatasetError, match="教師データセットのファイルではない"):
        load_dataset(write(tmp_path / "x.json", '{"type": "FeatureCollection"}'))
    with pytest.raises(DatasetError, match="読めず"):
        load_dataset(str(tmp_path / "none.json"))
    with pytest.raises(DatasetError, match="知らない注釈"):
        load_dataset(write(tmp_path / "y.json", json.dumps({"format": "satobs-dataset", "groups": [{"format": "kitti", "items": []}]})))
    assert Reader().boxes(Group("yolo"), Item("a.png")) == []
