"""土地被覆分類 (core.classify): the viewer's classify.test.ts, in NumPy."""
import numpy as np
import pytest

from satellite_observation.core.classify import (
    ClassModel, ClassStats, accumulate, class_color, classify, invert_spd, k_means, random, sample_pixels, train_model,
)


def scene(n):
    """Three bands of `n` pixels: dark, green and bright thirds, with a little noise."""
    looks = [(10, 20, 30), (40, 120, 50), (200, 180, 160)]
    bands = np.zeros((3, n), np.float32)
    for i in range(n):
        look = looks[3 * i // n]
        for b in range(3):
            bands[b, i] = look[b] + (i * (b + 1)) % 5 - 2
    return bands


def test_inverts_a_symmetric_positive_definite_matrix():
    inverse, log_det = invert_spd(np.array([[4.0, 2], [2, 3]]))
    assert np.round(inverse, 3).tolist() == [[0.375, -0.25], [-0.25, 0.5]]
    assert log_det == pytest.approx(np.log(8))
    assert invert_spd(np.array([[1.0, 2], [2, 1]])) is None


def test_the_random_numbers_are_the_viewers():
    rand = random(1)
    # mulberry32(1), as in JavaScript.
    assert [round(rand(), 6) for _ in range(3)] == [0.627074, 0.002736, 0.527447]


def test_k_means_finds_the_three_looks_darkest_first():
    bands = scene(300)
    samples = sample_pixels(bands, 300, random(1))
    means = k_means(samples, 3)
    assert [round(means[0, 0]), round(means[1, 1]), round(means[2, 0])] == [10, 120, 200]
    classes = classify(bands, ClassModel("distance", means))
    assert [classes[0], classes[150], classes[299]] == [1, 2, 3]
    # The same seed, the same classes.
    assert np.array_equal(k_means(samples, 3, seed=7), k_means(samples, 3, seed=7))
    with pytest.raises(ValueError, match="2 のみ"):
        k_means(samples[:2], 3)


def test_skips_pixels_without_a_value_in_every_band():
    bands = np.array([[1, np.nan, 3], [1, 2, 3]])
    assert classify(bands, ClassModel("distance", np.array([[0.0, 0], [5, 5]]))).tolist() == [1, 0, 2]
    assert sample_pixels(bands, 10, random(1)).shape == (2, 2)


def test_trains_on_labeled_pixels_by_distance_or_likelihood():
    n = 300
    bands = scene(n)
    labels = np.zeros(n, np.uint8)
    labels[10:30], labels[110:130], labels[210:230] = 1, 2, 3
    stats = ClassStats.empty(3, 3)
    accumulate(stats, bands, labels)
    assert stats.counts.tolist() == [20, 20, 20]
    for kind in ("distance", "likelihood"):
        classes = classify(bands, train_model(stats, kind, ["水", "植生", "裸地"]))
        assert [classes[i] for i in (0, 99, 100, 199, 200, 299)] == [1, 1, 2, 2, 3, 3]


def test_says_which_classes_have_too_few_training_pixels():
    stats = ClassStats.empty(2, 3)
    stats.counts[:] = [10, 2]
    with pytest.raises(ValueError, match="森"):
        train_model(stats, "likelihood", ["水", "森"])
    stats.sums[:] = 1
    train_model(stats, "distance", ["水", "森"])


def test_a_flat_class_is_still_invertible():
    # Every training pixel the same (a saturated area): the ridge keeps it.
    stats = ClassStats.empty(2, 2)
    accumulate(stats, np.array([[5.0] * 4 + [1, 2, 3, 4], [5.0] * 4 + [4, 2, 3, 1]]), np.array([1] * 4 + [2] * 4))
    model = train_model(stats, "likelihood", ["a", "b"])
    assert classify(np.array([[5.0, 2.5], [5.0, 2.5]]), model).tolist() == [1, 2]


def test_class_colours_go_round():
    assert class_color(1) == (31, 119, 180) and class_color(21) == class_color(1)
