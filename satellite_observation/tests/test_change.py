"""変化抽出: the viewer's change.test.ts cases on core.change, and the comparisons."""
import math

import numpy as np
import pytest

from satellite_observation.core.change import (
    Thresholds, change_mask, compare, describe_thresholds, mean_and_deviation, method_of, spread_of, thresholds_of,
)


def test_takes_the_mean_2_sigma_or_center_a_typed_threshold():
    values = np.array([0, 0, 0, 0, 10, np.nan], np.float32)
    mean, deviation, count = mean_and_deviation(values)
    assert (count, mean) == (5, 2) and deviation == pytest.approx(4)
    assert thresholds_of(values, 0, None) == Thresholds(2 - 8, 2 + 8)
    assert thresholds_of(values, 1, 0.5) == Thresholds(0.5, 1.5)
    assert thresholds_of([math.nan], 0, None) == Thresholds(-math.inf, math.inf)


def test_marks_the_pixels_that_changed_one_way():
    values = [-5, 0, 5, math.nan]
    assert change_mask(values, Thresholds(-1, 1), "increase").tolist() == [0, 0, 1, 0]
    assert change_mask(values, Thresholds(-1, 1), "decrease").tolist() == [1, 0, 0, 0]


def test_spreads_the_colors_over_most_of_the_distances_from_no_change():
    values = [i if i % 2 else -i for i in range(100)]
    assert spread_of(values, 0) == 98
    assert spread_of([1, 1, math.nan], 1) == 1


def test_compares_as_the_methods_say():
    a, b = np.array([10, 10, 0, np.nan]), np.array([20, 5, 0, 1])
    assert compare(a, b, method_of("difference")).tolist()[:3] == [10, -5, 0]
    assert compare(a, b, method_of("ratio"))[:2].tolist() == [2, 0.5] and np.isnan(compare(a, b, method_of("ratio"))[2:]).all()
    assert compare(a, b, method_of("logRatio"))[0] == pytest.approx(10 * math.log10(2))
    assert compare(a, b, method_of("normalized"))[0] == pytest.approx(1 / 3)
    assert describe_thresholds(Thresholds(-12.345, 15.6), "both", method_of("logRatio")) == "-12.3 dB 以下・15.6 dB 以上"
    assert describe_thresholds(Thresholds(0.5, 1.5), "increase", method_of("ratio")) == "1.5 以上"
