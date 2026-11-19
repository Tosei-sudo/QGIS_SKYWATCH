"""「保存した範囲」 of the image catalog: kept, replaced by name, renamed, deleted, read back whatever was written."""
import json

import pytest

from mikage.core.catalog.saved_areas import LIMIT, add_area, by_name, delete_area, find, read_areas, rename_area, write_areas

SQUARE = [[139.7, 35.6], [139.8, 35.6], [139.8, 35.7]]


def test_an_area_is_kept_closed_newest_first_and_read_back():
    areas, first = add_area([], "東京湾", SQUARE, "2026-11-19 12:00")
    areas, second = add_area(areas, "横須賀", [[139.6, 35.2], [139.7, 35.2], [139.7, 35.3], [139.7, 35.3]], "2026-11-19 12:05")
    assert [a.name for a in areas] == ["横須賀", "東京湾"]
    # The ring closed, a corner clicked twice in a row once.
    assert first.ring[0] == first.ring[-1] and len(first.ring) == 4
    assert len(second.ring) == 4
    again = read_areas(write_areas(areas))
    assert again == areas and again[1].saved == "2026-11-19 12:00"


def test_the_same_name_replaces_the_area_keeping_its_id():
    areas, first = add_area([], "東京湾", SQUARE, "2026-11-19 12:00")
    areas, _ = add_area(areas, "横須賀", SQUARE, "2026-11-19 12:05")
    areas, again = add_area(areas, " 東京湾 ", [[139.0, 35.0], [139.1, 35.0], [139.1, 35.1]], "2026-11-19 13:00")
    assert again.id == first.id and [a.name for a in areas] == ["東京湾", "横須賀"]
    assert by_name(areas, "東京湾").ring[0] == (139.0, 35.0)


def test_rename_and_delete():
    areas, a = add_area([], "東京湾", SQUARE, "")
    areas, b = add_area(areas, "横須賀", SQUARE, "")
    with pytest.raises(ValueError):
        rename_area(areas, a.id, "横須賀")
    with pytest.raises(ValueError):
        rename_area(areas, a.id, "  ")
    areas = rename_area(areas, a.id, "東京湾（北）")
    assert find(areas, a.id).name == "東京湾（北）" and [x.name for x in areas] == ["横須賀", "東京湾（北）"]
    areas = delete_area(areas, b.id)
    assert [x.name for x in areas] == ["東京湾（北）"]


def test_nothing_to_save_without_a_name_or_three_corners():
    with pytest.raises(ValueError):
        add_area([], "", SQUARE, "")
    with pytest.raises(ValueError):
        add_area([], "線", [[139.7, 35.6], [139.8, 35.6], [139.7, 35.6]], "")


def test_unreadable_settings_give_what_can_be_read():
    assert read_areas(None) == [] and read_areas("") == [] and read_areas("{") == [] and read_areas("[]") == []
    text = json.dumps({"areas": [
        {"id": "x", "name": "良い", "ring": SQUARE},
        {"id": "y", "name": "", "ring": SQUARE},
        {"id": "z", "name": "点", "ring": [[1, 2]]},
        {"id": "w", "name": "壊れた", "ring": [["a", "b"], [1, 2], [3, 4]]},
        "not an area",
        {"id": "x", "name": "同じ id", "ring": SQUARE},
    ]})
    assert [a.name for a in read_areas(text)] == ["良い"]


def test_at_most_limit_areas_are_kept():
    areas = []
    for i in range(LIMIT + 5):
        areas, _ = add_area(areas, f"範囲 {i}", SQUARE, "")
    assert len(areas) == LIMIT and areas[0].name == f"範囲 {LIMIT + 4}" and by_name(areas, "範囲 0") is None
