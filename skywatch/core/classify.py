"""
Land cover classification of a multi-band image, pixel by pixel (the
viewer's classify.ts): unsupervised (k-means over a sample of the pixels:
give the number of classes) or supervised (statistics of training polygons,
one class per value of an attribute, then the minimum distance to the class
means or the maximum likelihood of a Gaussian per class). Classes are
numbered from 1; 0 is no data.

Pixels are arrays of shape (dims, n): one row per band, NaN where a band has
no value (such a pixel is left out).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

#: Pixels k-means is trained on, over the whole image; most classes (8-bit class numbers).
MAX_SAMPLES = 60_000
MAX_CLASSES = 50
KINDS = ("distance", "likelihood")

#: Distinct colours for classes, in order (then again).
CLASS_COLORS = [
    (31, 119, 180), (44, 160, 44), (214, 39, 40), (255, 127, 14), (148, 103, 189), (140, 86, 75), (227, 119, 194),
    (127, 127, 127), (188, 189, 34), (23, 190, 207), (174, 199, 232), (152, 223, 138), (255, 152, 150), (255, 187, 120),
    (197, 176, 213), (196, 156, 148), (247, 182, 210), (199, 199, 199), (219, 219, 141), (158, 218, 229),
]


def class_color(c: int) -> tuple:
    """The colour of 1-based class `c`."""
    return CLASS_COLORS[(c - 1) % len(CLASS_COLORS)]


@dataclass
class ClassStats:
    """Statistics of training pixels per class: counts (classes), sums (classes × dims) and sums of products (classes × dims × dims)."""
    counts: np.ndarray
    sums: np.ndarray
    products: np.ndarray

    @classmethod
    def empty(cls, classes: int, dims: int) -> "ClassStats":
        return cls(np.zeros(classes), np.zeros((classes, dims)), np.zeros((classes, dims, dims)))

    def add(self, other: "ClassStats") -> None:
        self.counts += other.counts
        self.sums += other.sums
        self.products += other.products


@dataclass
class ClassModel:
    """A trained classifier: per class its mean, and for maximum likelihood its inverse covariance and the constant of its log density."""
    kind: str
    means: np.ndarray
    inverses: Optional[np.ndarray] = None
    constants: Optional[np.ndarray] = None


def _valid(values: np.ndarray) -> np.ndarray:
    return ~np.isnan(values).any(axis=0)


def accumulate(stats: ClassStats, values, labels) -> None:
    """Adds the pixels where `labels` (0: none, else class + 1) is set and every band has a value."""
    values = np.asarray(values, dtype=np.float64)
    labels = np.asarray(labels).ravel()
    keep = (labels > 0) & _valid(values)
    x = values[:, keep].T
    c = labels[keep].astype(int) - 1
    for k in np.unique(c):
        xs = x[c == k]
        stats.counts[k] += len(xs)
        stats.sums[k] += xs.sum(axis=0)
        stats.products[k] += xs.T @ xs


def min_pixels(kind: str, dims: int) -> int:
    """Fewest training pixels a class needs: one more than the bands for maximum likelihood (a covariance), one for minimum distance."""
    return dims + 1 if kind == "likelihood" else 1


def invert_spd(m: np.ndarray) -> Optional[tuple[np.ndarray, float]]:
    """The inverse and log det of a symmetric positive definite matrix, None when it is not positive definite."""
    try:
        lower = np.linalg.cholesky(m)
    except np.linalg.LinAlgError:
        return None
    inverse_lower = np.linalg.inv(lower)
    return inverse_lower.T @ inverse_lower, float(2 * np.log(np.diag(lower)).sum())


def train_model(stats: ClassStats, kind: str, names: Sequence[str]) -> ClassModel:
    """A classifier from training statistics. Raises ValueError naming the classes with too few pixels."""
    dims = stats.sums.shape[1]
    few = [names[c] for c in range(len(stats.counts)) if stats.counts[c] < min_pixels(kind, dims)]
    if few:
        raise ValueError(f"学習用の画素が不足のクラス: {'、'.join(few)}（{min_pixels(kind, dims)} 画素以上必要）")
    means = stats.sums / stats.counts[:, None]
    if kind == "distance":
        return ClassModel(kind, means)
    inverses = np.zeros_like(stats.products)
    constants = np.zeros(len(stats.counts))
    for c in range(len(stats.counts)):
        cov = stats.products[c] / stats.counts[c] - np.outer(means[c], means[c])
        # A little ridge keeps a flat class (a uniform area, a saturated band) invertible.
        cov = cov + np.eye(dims) * max(1e-9, 1e-4 * float(np.trace(cov)) / dims)
        inv = invert_spd(cov)
        if inv is None:
            raise ValueError(f"クラス「{names[c]}」の分散の算出に失敗（学習用の画素の値がそろいすぎ）")
        inverses[c], constants[c] = inv[0], -0.5 * inv[1]
    return ClassModel(kind, means, inverses, constants)


def classify(values, model: ClassModel) -> np.ndarray:
    """Every pixel in the class it fits best: 1-based, 0 where a band has no value (uint8)."""
    values = np.asarray(values, dtype=np.float64)
    ok = _valid(values)
    x = values[:, ok].T
    out = np.zeros(values.shape[1], np.uint8)
    if not len(x):
        return out
    scores = np.empty((len(x), len(model.means)))
    for c, mean in enumerate(model.means):
        diff = x - mean
        if model.kind == "distance":
            scores[:, c] = -(diff * diff).sum(axis=1)
        else:
            scores[:, c] = model.constants[c] - 0.5 * np.einsum("ij,jk,ik->i", diff, model.inverses[c], diff)
    out[ok] = (np.argmax(scores, axis=1) + 1).astype(np.uint8)
    return out


def random(seed: int):
    """A small seeded random generator (mulberry32, as the viewer's), so a classification can be made again the same."""
    state = [seed & 0xFFFFFFFF]

    def imul(a, b):
        return (a * b) & 0xFFFFFFFF

    def next_value() -> float:
        state[0] = (state[0] + 0x6D2B79F5) & 0xFFFFFFFF
        t = state[0]
        t = imul(t ^ (t >> 15), t | 1)
        t ^= (t + imul(t ^ (t >> 7), t | 61)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296
    return next_value


def sample_pixels(values, count: int, rand) -> np.ndarray:
    """Up to `count` pixels with a value in every band, picked evenly with a random start: shape (take, dims)."""
    values = np.asarray(values, dtype=np.float64)
    valid = np.flatnonzero(_valid(values))
    take = min(count, len(valid))
    step = len(valid) / max(1, take)
    start = rand() * step
    picks = valid[np.minimum(len(valid) - 1, np.floor(start + np.arange(take) * step).astype(int))] if take else valid[:0]
    return values[:, picks].T.astype(np.float32)


def k_means(samples: np.ndarray, k: int, iterations: int = 30, seed: int = 1) -> np.ndarray:
    """
    k-means of `samples` (n × dims) into `k` clusters: k-means++ seeds, then
    Lloyd's iterations until no sample changes cluster (or `iterations`). The
    clusters come out from darkest to brightest (by the sum of their mean), so
    class 1 is the darkest, the same each time.
    """
    samples = np.asarray(samples, dtype=np.float64)
    n, dims = samples.shape
    if n < k:
        raise ValueError(f"値のある画素が {n} のみのため、{k} クラスへの分類不可")
    rand = random(seed)
    means = np.zeros((k, dims))
    nearest = np.full(n, np.inf)
    pick = math.floor(rand() * n)
    for c in range(k):
        means[c] = samples[pick]
        nearest = np.minimum(nearest, ((samples - means[c]) ** 2).sum(axis=1))
        total = float(nearest.sum())
        if c == k - 1:
            break
        if not total > 0:
            pick = math.floor(rand() * n)
            continue
        r = rand() * total
        hit = np.flatnonzero(np.cumsum(nearest) >= r)
        pick = int(hit[0]) if len(hit) else n - 1
    owner = np.full(n, -1)
    for _ in range(iterations):
        d = ((samples[:, None, :] - means[None, :, :]) ** 2).sum(axis=2)
        best = np.argmin(d, axis=1)
        moved = int((best != owner).sum())
        owner = best
        for c in range(k):
            members = samples[owner == c]
            if not len(members):
                # An empty cluster takes the sample farthest from its own mean.
                own = ((samples - means[owner]) ** 2).sum(axis=1)
                means[c] = samples[int(np.argmax(own))]
                moved += 1
                continue
            means[c] = members.mean(axis=0)
        if not moved:
            break
    return means[np.argsort(means.sum(axis=1), kind="stable")]
