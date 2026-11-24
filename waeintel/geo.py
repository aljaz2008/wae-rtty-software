"""Geodesy helpers: Maidenhead grids, great-circle bearing/distance, solar elevation."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

EARTH_KM = 6371.0


def grid_to_latlon(grid: str):
    """Maidenhead locator (4/6/8 chars) -> (lat, lon) of square centre."""
    g = grid.strip().upper()
    if not re.fullmatch(r"[A-R]{2}\d{2}([A-X]{2}(\d{2})?)?", g):
        raise ValueError(f"bad grid {grid!r}")
    lon = (ord(g[0]) - 65) * 20 - 180
    lat = (ord(g[1]) - 65) * 10 - 90
    lon += int(g[2]) * 2
    lat += int(g[3]) * 1
    dlon, dlat = 2.0, 1.0
    if len(g) >= 6:
        lon += (ord(g[4]) - 65) * (2 / 24)
        lat += (ord(g[5]) - 65) * (1 / 24)
        dlon, dlat = 2 / 24, 1 / 24
    if len(g) >= 8:
        lon += int(g[6]) * (2 / 240)
        lat += int(g[7]) * (1 / 240)
        dlon, dlat = 2 / 240, 1 / 240
    return lat + dlat / 2, lon + dlon / 2


def bearing_distance(lat1, lon1, lat2, lon2):
    """Short-path initial bearing (deg, 0..360) and distance (km)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = (math.degrees(math.atan2(x, y)) + 360) % 360
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    dist = 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))
    return brg, dist


def solar_elevation(lat, lon, when: datetime | None = None):
    """Approximate solar elevation angle in degrees (NOAA simplified)."""
    when = when or datetime.now(timezone.utc)
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cosz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cosz))))


def sun_state(elev):
    if elev > 6:
        return "day"
    if elev > -12:
        return "grey"
    return "night"


def sunrise_sunset(lat, lon, day: datetime | None = None):
    """Today's sunrise/sunset (UTC 'HH:MM'), by scanning for -0.833 deg crossings."""
    d = (day or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return _rise_set(round(lat, 3), round(lon, 3), d)


@lru_cache(maxsize=32)
def _rise_set(lat, lon, d):
    day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    rise = sset = None
    prev = solar_elevation(lat, lon, day)
    for m in range(1, 24 * 60 + 1):
        t = day + timedelta(minutes=m)
        e = solar_elevation(lat, lon, t)
        if prev < -0.833 <= e and rise is None:
            rise = t.strftime("%H:%M")
        if prev >= -0.833 > e and sset is None:
            sset = t.strftime("%H:%M")
        prev = e
    return rise, sset


def circular_mean(angles_weights):
    sx = sy = 0.0
    for a, w in angles_weights:
        sx += math.sin(math.radians(a)) * w
        sy += math.cos(math.radians(a)) * w
    if sx == 0 and sy == 0:
        return None
    return (math.degrees(math.atan2(sx, sy)) + 360) % 360


def compass(brg):
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((brg + 11.25) // 22.5) % 16]


# Rough call-area centroids for big countries, so bearings to W6 vs W1 differ.
CALL_AREA = {
    "K": {"1": (42.5, -71.8), "2": (41.2, -74.6), "3": (40.2, -77.2), "4": (34.5, -83.0),
          "5": (32.5, -97.0), "6": (36.8, -119.8), "7": (43.5, -114.0), "8": (40.8, -82.8),
          "9": (41.8, -88.5), "0": (41.5, -98.0)},
    "VE": {"1": (45.5, -63.5), "2": (47.0, -72.0), "3": (44.5, -79.5), "4": (50.0, -97.0),
           "5": (52.0, -106.0), "6": (53.5, -114.0), "7": (49.5, -123.0), "9": (46.0, -66.0)},
    "VK": {"1": (-35.3, 149.1), "2": (-33.5, 150.5), "3": (-37.5, 145.0), "4": (-24.0, 150.0),
           "5": (-34.5, 138.6), "6": (-31.5, 116.5), "7": (-42.0, 147.0), "8": (-15.0, 133.0)},
    "PY": {"1": (-22.9, -43.2), "2": (-23.5, -46.6), "3": (-30.0, -51.2), "4": (-19.9, -43.9),
           "5": (-25.4, -49.3), "6": (-12.9, -38.5), "7": (-8.0, -35.0), "8": (-1.5, -48.5)},
}
