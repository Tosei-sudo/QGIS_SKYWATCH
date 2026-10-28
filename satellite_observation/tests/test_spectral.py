"""スペクトル: the viewer's spectral.test.ts cases on core.spectral."""
import math

import numpy as np
import pytest

from satellite_observation.core.spectral import (
    BandStat, Spectrum, axis_range, band_labels, band_stats, box_label, format_value, pixel_window, read_size, spectral_csv,
)


def test_band_stats_without_no_data():
    a, b = band_stats([np.array([1, 2, 3, np.nan], np.float32), np.array([0, 10, 20, 0], np.uint16)], lambda v: v == 0)
    assert (a.mean, a.min, a.max, a.count) == (2, 1, 3, 3)
    assert a.std == pytest.approx(math.sqrt(2 / 3))
    assert (b.mean, b.min, b.max, b.std, b.count) == (15, 10, 20, 5, 2)


def test_a_band_of_no_data_only_has_no_statistics():
    [s] = band_stats([[0, 0]], lambda v: v == 0)
    assert s.count == 0 and math.isnan(s.mean)


def test_band_labels():
    assert band_labels(3, ["Blue", None]) == ["B1 Blue", "B2", "B3"]


def stat(mean, extra=0):
    return BandStat(mean, mean - extra, mean + extra, extra, 1)


def test_spectral_csv():
    series = [Spectrum(1, "点（列 3、行 4）", "#000", [stat(50), stat(80)], 1), Spectrum(2, "範囲 2 × 2 画素", "#000", [stat(10, 1), stat(20, 2)], 4)]
    assert spectral_csv(series, ["B1", "B2"]) == (
        "band,1 点（列 3、行 4）,2 範囲 2 × 2 画素,2 範囲 2 × 2 画素 min,2 範囲 2 × 2 画素 max,2 範囲 2 × 2 画素 std\nB1,50,10,9,11,1\nB2,80,20,18,22,2\n")


def test_axis_windows_and_formats():
    assert axis_range([Spectrum(1, "", "#000", [stat(50), stat(80)], 1)]) == (50, 80, 10)
    assert axis_range([]) is None
    assert pixel_window([-3.2, 10.5], [2.1, 4.9], 8, 8) == (0, 2, 8, 5)
    assert pixel_window([9, 12], [0, 1], 8, 8) is None
    assert read_size(2000, 2000) == (1000, 1000, True) and read_size(10, 10) == (10, 10, False)
    assert box_label(1200, 30, True) == "範囲 1,200 × 30 画素（間引き）"
    assert [format_value(v) for v in (0.12345, 512.25, 12345.0, 0.0012, 3.0)] == ["0.1235", "512.25", "12345", "1.20e-3", "3"]
