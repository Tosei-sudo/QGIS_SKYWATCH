"""
Cloud cover forecasts for optical shots (`cloudForecast`), as the viewer
(imaging-plan-weather.ts). Nothing is asked until the person turns the
forecast on, as the targets' places go to the service.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlencode

from ..util import is_number, is_record, is_url, time_of


@dataclass(frozen=True)
class CloudForecastConfig:
    """An Open-Meteo-style forecast API."""
    url: str
    label: str


DEFAULT_CLOUD_FORECAST = CloudForecastConfig("https://api.open-meteo.com/v1/forecast", "Open-Meteo")
#: How far ahead forecasts reach, days.
FORECAST_DAYS = 16


def cloud_forecast_of(value: Any, problems: list[str]) -> Optional[CloudForecastConfig]:
    """`cloudForecast`: an object with `url` (and `label`), or false for none."""
    if value is False or value is None:
        return None
    if is_record(value):
        url, label = value.get("url"), value.get("label")
        if is_url(url):
            return CloudForecastConfig(url, label if isinstance(label, str) and label else "雲量予報")
    problems.append('cloudForecast は { "url": "…/v1/forecast" } か false で指定')
    return DEFAULT_CLOUD_FORECAST
#: Places closer than this (degrees) share one forecast.
GRID = 0.25
#: The most places asked for in one request.
PER_REQUEST = 50


@dataclass
class CloudSeries:
    """One place's hourly cloud cover: times (ms) and percents (NaN where missing)."""
    times: list
    cover: list


def cloud_at(series: CloudSeries, ms: float) -> Optional[float]:
    """The cover at `ms`, between the hours either side; None outside the forecast."""
    times, cover = series.times, series.cover
    if not times or ms < times[0] or ms > times[-1]:
        return None
    lo, hi = 0, len(times) - 1
    while hi - lo > 1:
        mid = (lo + hi) >> 1
        if times[mid] <= ms:
            lo = mid
        else:
            hi = mid
    a, b = cover[lo], cover[hi]
    if not (math.isfinite(a) and math.isfinite(b)):
        return a if math.isfinite(a) else b if math.isfinite(b) else None
    f = 0 if times[hi] == times[lo] else (ms - times[lo]) / (times[hi] - times[lo])
    return a + (b - a) * f


def _js_round(x: float) -> float:
    return math.floor(x + 0.5)


def key_of(place: Sequence[float]) -> str:
    """The key of the grid cell a place (longitude, latitude) falls in (as the viewer writes it)."""
    lon, lat = place[0], place[1]
    return f"{_number_text(_js_round(lat / GRID) * GRID)},{_number_text(_js_round(lon / GRID) * GRID)}"


def _number_text(v: float) -> str:
    """A number as JavaScript writes it (`35.75`, `140`, `0`)."""
    if v == 0:
        return "0"
    return str(int(v)) if v == int(v) else repr(v)


def _series_of(entry: Any) -> Optional[CloudSeries]:
    hourly = entry.get("hourly") if isinstance(entry, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list) or not isinstance(hourly.get("cloud_cover"), list):
        return None
    times = [t * 1000 if is_number(t) else time_of(f"{t}Z") for t in hourly["time"]]
    cover = [float(v) if is_number(v) else math.nan for v in hourly["cloud_cover"]]
    return CloudSeries(times, cover)


#: Asks a URL (GET) and returns the body; raises with the reason when it cannot.
Fetch = Callable[[str], bytes]


def forecast_urls(config: CloudForecastConfig, places: Sequence[Sequence[float]]) -> list[tuple[list[str], str]]:
    """The requests for places (longitude, latitude): the grid cells of each, and its URL."""
    cells: dict[str, tuple[float, float]] = {}
    for p in places:
        key = key_of(p)
        if key not in cells:
            lat, lon = (float(x) for x in key.split(","))
            cells[key] = (lon, lat)
    keys = list(cells)
    out = []
    for i in range(0, len(keys), PER_REQUEST):
        batch = keys[i:i + PER_REQUEST]
        params = urlencode({
            "latitude": ",".join(f"{cells[k][1]:.2f}" for k in batch),
            "longitude": ",".join(f"{cells[k][0]:.2f}" for k in batch),
            "hourly": "cloud_cover",
            "forecast_days": str(FORECAST_DAYS),
            "timezone": "GMT",
            "timeformat": "unixtime",
        })
        out.append((batch, f"{config.url}{'&' if '?' in config.url else '?'}{params}"))
    return out


def fetch_clouds(config: CloudForecastConfig, places: Sequence[Sequence[float]], fetch: Fetch,
                 cancelled: Optional[Callable[[], bool]] = None) -> dict[str, CloudSeries]:
    """The forecasts for places (longitude, latitude), by grid cell, asked for in batches."""
    out: dict[str, CloudSeries] = {}
    for batch, url in forecast_urls(config, places):
        if cancelled and cancelled():
            break
        try:
            value = json.loads(fetch(url).decode("utf-8-sig"))
        except ValueError:
            raise ValueError(f"{config.label} の応答が JSON 以外") from None
        entries = value if isinstance(value, list) else [value]
        for k, key in enumerate(batch):
            series = _series_of(entries[k]) if k < len(entries) else None
            if series:
                out[key] = series
    return out


def apply_clouds(ops: Sequence, sats: Sequence, targets: Sequence, forecasts: dict[str, CloudSeries]) -> int:
    """
    Tells the optical satellites' opportunities the forecast cover at their
    moment (`cloud`, percent); those outside the forecast, or of SAR
    satellites, are left without. Returns how many were told.
    """
    n = 0
    for op in ops:
        op.cloud = None
        if sats[op.satellite].sar:
            continue
        series = forecasts.get(key_of(targets[op.target].center))
        cover = cloud_at(series, op.time) if series else None
        if cover is not None:
            op.cloud = _js_round(cover)
            n += 1
    return n


def places_for(ops: Sequence, sats: Sequence, targets: Sequence, now: float) -> list[tuple]:
    """The places whose forecasts the optical opportunities need (those within the forecast's reach of `now`)."""
    until = now + FORECAST_DAYS * 86400000
    seen: dict[int, None] = {}
    for op in ops:
        if not sats[op.satellite].sar and now - 3600000 <= op.time <= until:
            seen[op.target] = None
    return [tuple(targets[t].center) for t in seen]
