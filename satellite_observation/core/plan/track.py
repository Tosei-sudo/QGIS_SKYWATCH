"""
The ground track and what a satellite can image (撮像可能範囲) over a moment
or a period, for the 「衛星パス」 dock, as the viewer (satellite-track.ts and
satellite-track-panel.ts): the sub-satellite points, and on each side it can
look to the band between its smallest and largest off-nadir angle.

Coordinates are longitude, latitude. `wrap_line` and `wrap_polygon` cut what
crosses the antimeridian into pieces within ±180°.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .access import satrec_of
from .orbit import sun_elevation
from .satellites import SatelliteSpec, satellite_label
from .scene import ground_state_at

#: The longest period drawn, days.
MAX_DAYS = 7
#: The colours of the satellites, in the order they are chosen.
TRACK_COLORS = ["#e11d48", "#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4f46e5"]
#: Minutes between time marks: the smallest giving at most MAX_TICKS marks on a track.
MAX_TICKS = 48
TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440]


@dataclass
class ReachBand:
    """One band of the reach: the inner and outer edges, sample by sample (same length)."""
    side: str
    inner: list
    outer: list


@dataclass
class SatelliteTrack:
    sat: SatelliteSpec
    start: float
    end: float
    #: The sample times (ms) and where the satellite is over the ground then.
    times: list = field(default_factory=list)
    track: list = field(default_factory=list)
    #: Height above the ellipsoid, metres.
    heights: list = field(default_factory=list)
    ascending: list = field(default_factory=list)
    #: Whether the satellite can image at each sample (daylight, for optical ones when asked).
    open: list = field(default_factory=list)
    #: The reach bands, each cut where it cannot image.
    bands: list = field(default_factory=list)
    #: The smallest and largest off-nadir angles of the bands (degrees).
    offNadir: tuple = (0.0, 0.0)


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def step_for(ms: float) -> int:
    """Seconds between samples for a period: about 3000 samples, 10 s to 60 s apart."""
    return min(60, max(10, _js_round(ms / 1000 / 3000)))


def tick_minutes(minutes: float) -> int:
    """Minutes between time marks for a track of `minutes`."""
    return next((m for m in TICK_STEPS if minutes / m <= MAX_TICKS), TICK_STEPS[-1])


def reach_angles(sat: SatelliteSpec, max_off_nadir: Optional[float] = None) -> list[tuple]:
    """
    The off-nadir angles (left positive) bounding each band a satellite can
    image, as (side, inner, outer): both sides from the nadir in one band
    when it has no smallest angle, else a band on each side it can look to.
    """
    limit = sat.maxOffNadir if max_off_nadir is None else max_off_nadir
    hi = max(0.0, min(sat.maxOffNadir, limit))
    lo = min(hi, max(0.0, sat.minOffNadir))
    if sat.lookSide == "both" and lo == 0:
        return [("both", -hi, hi)]
    sides = ["left", "right"] if sat.lookSide == "both" else [sat.lookSide]
    return [(s, lo, hi) if s == "left" else (s, -lo, -hi) for s in sides]


def satellite_track(sat: SatelliteSpec, start: float, end: float, step: Optional[float] = None,
                    max_off_nadir: Optional[float] = None, min_sun: Optional[float] = None) -> Optional[SatelliteTrack]:
    """The track and reach of `sat` from `start` to `end` (ms); None when its TLE cannot be read."""
    orbit = satrec_of(sat)
    if isinstance(orbit, str):
        return None
    step_ms = (step if step is not None else step_for(end - start)) * 1000
    angles = reach_angles(sat, max_off_nadir)
    off_nadirs = [a for _, inner, outer in angles for a in (inner, outer)]
    result = SatelliteTrack(sat, start, end, offNadir=(min(abs(a[1]) for a in angles), max(abs(a[2]) for a in angles)))
    edges = [([], []) for _ in angles]
    n = max(1, math.ceil((end - start) / step_ms))
    for i in range(n + 1):
        t = min(end, start + i * step_ms)
        g = ground_state_at(orbit, t, off_nadirs)
        if g is None:
            continue
        result.times.append(t)
        result.track.append(g.lon_lat)
        result.heights.append(g.height * 1000)
        result.ascending.append(g.ascending)
        result.open.append(min_sun is None or sat.sar or sun_elevation(g.lon_lat, t) >= min_sun)
        for k, (inner, outer) in enumerate(edges):
            inner.append(g.across[2 * k])
            outer.append(g.across[2 * k + 1])
    # Each band in runs of samples where the satellite can image.
    for k, (side, _, _) in enumerate(angles):
        begin = -1
        for i in range(len(result.open) + 1):
            if i < len(result.open) and result.open[i]:
                if begin < 0:
                    begin = i
                continue
            if begin >= 0 and i - begin > 1:
                result.bands.append(ReachBand(side, edges[k][0][begin:i], edges[k][1][begin:i]))
            begin = -1
    return result


def sample_at(track: SatelliteTrack, ms: float) -> int:
    """The index of the sample nearest a moment; -1 when there are none."""
    best, gap = -1, math.inf
    for i, t in enumerate(track.times):
        if abs(t - ms) < gap:
            best, gap = i, abs(t - ms)
    return best


def _unwrapped(coords: Sequence) -> list:
    """Longitudes made continuous (no jumps of more than 180°)."""
    out: list = []
    for lon, lat in coords:
        out.append((lon + _js_round((out[-1][0] - lon) / 360) * 360 if out else lon, lat))
    return out


def _worlds(coords: Sequence) -> list:
    """Which copies of the world (…, −1, 0, 1, …: 360° apart) longitudes reach."""
    lo = min((c[0] for c in coords), default=math.inf)
    hi = max((c[0] for c in coords), default=-math.inf)
    if not math.isfinite(lo):
        return []
    out = []
    k = _js_round(lo / 360)
    while k * 360 - 180 < hi:
        if k * 360 + 180 > lo:
            out.append(k)
        k += 1
    return out


def _cut(a, b, x: float) -> tuple:
    return (x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))


def wrap_line(coords: Sequence) -> list:
    """A line (longitude, latitude) in pieces within ±180°, cut where it crosses the antimeridian."""
    line = _unwrapped(coords)
    pieces = []
    for k in _worlds(line):
        w, e = k * 360 - 180, k * 360 + 180

        def inside(p):
            return w <= p[0] <= e

        piece: list = []
        for i, p in enumerate(line):
            q = line[i - 1] if i else None
            if inside(p):
                if q and not inside(q):
                    piece.append(_cut(q, p, w if q[0] < w else e))
                piece.append(p)
            elif q and inside(q):
                piece.append(_cut(q, p, w if p[0] < w else e))
                if len(piece) > 1:
                    pieces.append([(lon - k * 360, lat) for lon, lat in piece])
                piece = []
        if len(piece) > 1:
            pieces.append([(lon - k * 360, lat) for lon, lat in piece])
    return pieces


def wrap_polygon(ring: Sequence) -> list:
    """A ring (longitude, latitude; closed or not) in pieces within ±180°, clipped at the antimeridian."""
    ring = [tuple(p) for p in ring]
    opened = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    every = _unwrapped(opened)
    out = []
    for k in _worlds(every):
        poly = every
        # Sutherland–Hodgman against the two edges of this world.
        for x, keep in ((k * 360 - 180, lambda p, x0=k * 360 - 180: p[0] >= x0), (k * 360 + 180, lambda p, x1=k * 360 + 180: p[0] <= x1)):
            nxt = []
            for i, p in enumerate(poly):
                q = poly[i - 1]
                if keep(p):
                    if not keep(q):
                        nxt.append(_cut(q, p, x))
                    nxt.append(p)
                elif keep(q):
                    nxt.append(_cut(q, p, x))
            poly = nxt
        if len(poly) < 3:
            continue
        shifted = [(lon - k * 360, lat) for lon, lat in poly]
        shifted.append(shifted[0])
        out.append(shifted)
    return out


def band_rings(band: ReachBand, chunk: int = 30) -> list:
    """A band as rings of up to `chunk` samples along it (each joined to the next), for drawing."""
    rings = []
    n = len(band.inner)
    for i in range(0, n - 1, chunk):
        j = min(n - 1, i + chunk)
        inner = _unwrapped(band.inner[i:j + 1])
        outer = _unwrapped(band.outer[i:j + 1])
        # The outer edge in the same world as the inner one.
        shift = _js_round((inner[0][0] - outer[0][0]) / 360) * 360
        rings.append([*inner, *[(lon + shift, lat) for lon, lat in reversed(outer)], inner[0]])
    return rings


def track_rows(tracks: Sequence[SatelliteTrack], at: Optional[float]) -> list[list[str]]:
    """One row a satellite: its name, where it is (at the moment, or at the start), height, which way it goes, and its reach."""

    def r(v: float, digits: int = 1) -> str:
        p = 10 ** digits
        x = math.floor(v * p + 0.5) / p
        return f"{x:.{digits}f}".rstrip("0").rstrip(".") if digits else str(int(x))

    rows = []
    for track in tracks:
        i = 0 if at is None else sample_at(track, at)
        lon, lat = track.track[i]
        lo, hi = track.offNadir
        reach = f"{r(lo)}〜{r(hi)}°" if lo > 0 else f"{r(hi)}° 以下"
        side = {"both": "左右", "right": "右"}.get(track.sat.lookSide, "左")
        rows.append([satellite_label(track.sat), f"{r(lat, 3)}, {r(lon, 3)}", f"{r(track.heights[i] / 1000, 0)} km",
                     "北行" if track.ascending[i] else "南行", f"{reach}・{side}"])
    return rows


def track_ticks(track: SatelliteTrack, at: Optional[float]) -> list[tuple]:
    """The time marks of a track: (sample index, ms), skipping the one at the moment shown."""
    every = tick_minutes((track.end - track.start) / 60000) * 60000
    nxt = math.ceil(track.start / every) * every
    out = []
    for i, t in enumerate(track.times):
        if t < nxt:
            continue
        nxt += every
        if at is not None and abs(t - at) < every / 2:
            continue
        out.append((i, t))
    return out


def track_shapes(track: SatelliteTrack) -> tuple:
    """The ground track as lines and the reach as rings, cut at the antimeridian."""
    lines = wrap_line(track.track)
    rings = [part for band in track.bands for ring in band_rings(band) for part in wrap_polygon(ring)]
    return lines, rings


def track_layers(tracks: Sequence[SatelliteTrack]) -> tuple:
    """Tracks as two layers' contents: ground tracks (lines) and what each can image (areas). (fields, lines, areas)"""
    from .output import LayerField

    fields = [LayerField("kind", "string", "種類"), LayerField("satellite", "string", "衛星"), LayerField("satellite_id", "string", "衛星ID"),
              LayerField("start", "date", "開始"), LayerField("end", "date", "終了"),
              LayerField("min_off_nadir", "double", "最小オフナディア角"), LayerField("max_off_nadir", "double", "最大オフナディア角")]
    lines, areas = [], []
    for track in tracks:
        base = {"satellite": satellite_label(track.sat), "satellite_id": track.sat.id, "start": track.start, "end": track.end,
                "min_off_nadir": math.floor(track.offNadir[0] * 100 + 0.5) / 100, "max_off_nadir": math.floor(track.offNadir[1] * 100 + 0.5) / 100}
        line_parts, rings = track_shapes(track)
        if line_parts:
            lines.append((line_parts, {**base, "kind": "地上軌跡"}))
        if rings:
            areas.append((rings, {**base, "kind": "撮像可能範囲"}))
    return fields, lines, areas
