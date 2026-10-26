"""
Cloud cover forecasts for optical shots (`cloudForecast`), as the viewer
(imaging-plan-weather.ts). Nothing is asked until the person turns the
forecast on, as the targets' places go to the service.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..util import is_record, is_url


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
