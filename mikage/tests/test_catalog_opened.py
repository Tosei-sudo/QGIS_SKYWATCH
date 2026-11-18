"""
開いた画像の履歴 (core/catalog/opened.py, tsudo 2026-11-18): kept in a file,
the newest first, one per image of a catalog, at most LIMIT; the files
opened and found; what is no longer there.
"""
import json

from mikage.core.catalog.opened import FILES_LIMIT, OpenedImage, OpenedImages


def entry(image_id, place, files=(), opened=(), url="https://x/0"):
    return OpenedImage(image_id=image_id, catalog_url=url, catalog_label="衛星画像", place=str(place), is_url=False,
                       given="\\\\nas\\x", files=list(files), opened=list(opened), feature={"attributes": {"IMG_ID": image_id}})


def test_the_newest_first_one_per_image_of_a_catalog_and_kept_in_the_file(tmp_path):
    path = tmp_path / "sub" / "opened.json"
    book = OpenedImages(str(path), limit=3)
    assert book.items() == [] and book.find("A") is None
    book.add(entry("A", tmp_path / "a.tif"), now=1)
    book.add(entry("B", tmp_path / "b.tif"), now=2)
    book.add(entry("A", tmp_path / "a2.tif"), now=3)
    book.add(entry("A", tmp_path / "a.tif", url="https://y/0"), now=4)
    assert [(e.image_id, e.catalog_url, e.opened_at) for e in book.items()] == [("A", "https://y/0", 4), ("A", "https://x/0", 3), ("B", "https://x/0", 2)]
    assert book.find("A").catalog_url == "https://y/0" and book.find("A", "https://x/0").place.endswith("a2.tif")
    book.add(entry("C", tmp_path / "c.tif"), now=5)
    assert [e.image_id for e in book.items()] == ["C", "A", "A"]
    # Read again from the file, as written.
    again = OpenedImages(str(path))
    assert [e.to_json() for e in again.items()] == [e.to_json() for e in book.items()]
    assert again.recent(1)[0].image_id == "C"
    again.remove(again.items()[0])
    assert [e.image_id for e in OpenedImages(str(path)).items()] == ["A", "A"]


def test_a_broken_or_odd_file_is_read_as_far_as_it_goes(tmp_path):
    path = tmp_path / "opened.json"
    path.write_text("{not json", encoding="utf-8")
    assert OpenedImages(str(path)).items() == []
    path.write_text(json.dumps([{"image_id": "A"}, 3, {"image_id": "B", "place": "/x/b.tif", "files": ["a", 1], "acquired": "x"},
                                {"image_id": "B", "place": "/x/other.tif"}]), encoding="utf-8")
    [b] = OpenedImages(str(path)).items()
    assert b.image_id == "B" and b.place == "/x/b.tif" and b.files == ["a"] and b.acquired is None


def test_what_is_no_longer_there(tmp_path):
    (tmp_path / "scene" / "PAN").mkdir(parents=True)
    (tmp_path / "scene" / "PAN" / "a.tif").write_bytes(b"x")
    e = entry("D", tmp_path / "scene", ["PAN/a.tif", "PAN/b.tif"], ["PAN/a.tif"])
    assert e.missing() == [] and e.missing(["PAN/a.tif", "PAN/b.tif"]) == ["PAN/b.tif"]
    assert e.paths(["PAN/a.tif"]) == [str(tmp_path / "scene" / "PAN" / "a.tif")]
    one = entry("E", tmp_path / "e.tif")
    assert one.missing() == ["e.tif"] and one.paths() == [str(tmp_path / "e.tif")]
    url = OpenedImage("F", "u", "c", "https://x.example/f.tif", True, "")
    assert url.missing() == []


def test_a_folder_of_many_files_keeps_those_opened(tmp_path):
    book = OpenedImages(str(tmp_path / "opened.json"))
    many = [f"f{i:04}.tif" for i in range(FILES_LIMIT + 50)]
    kept = book.add(entry("G", tmp_path, many, ["f0249.tif"]))
    assert len(kept.files) == FILES_LIMIT and kept.files[0] == "f0249.tif" and kept.opened == ["f0249.tif"]
