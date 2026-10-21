# -*- coding: utf-8 -*-
"""Synthetic acquired burial data: deterministic deliveries for tests and examples.

No real delivery is shipped, so these stand in for the usual formats:

* :func:`plough_run` + :func:`daily_files`: a plough log at a chosen rate,
  cut into UTC-day files that run 10 minutes past midnight (the overlap a
  daily delivery usually has). The run has stops, a short reverse, a
  logging gap, and on day 2 a few bad cells (``n/a``, ``#VALUE!``, a line
  cut short). :func:`redelivered_day` is the same day re-sent with a DOL
  correction and the bad cells fixed.
* :func:`survey_listing`: a KP-based post-lay survey listing (no time
  column: the acquisition date is given on import).
* :func:`rov_resurvey`: a short later survey in another house style
  (semicolons, decimal commas, KP in metres, DOL in cm, separate date and
  time columns, positions in degrees and decimal minutes).

Positions lie along :data:`ROUTE_LONLAT` (WGS 84) on the plugin's KP path:
the WGS 84 geodesic between vertices, KP the distance along it, and DCC
measured along the geodesic at right angles (starboard of increasing KP is
positive). KP therefore checks out against an RPL built from the same
vertices. Eastings and northings are UTM zone 31N (EPSG:32631).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ROUTE_LONLAT: Tuple[Tuple[float, float], ...] = ((2.80, 54.90), (2.85, 54.99), (2.99, 55.06), (3.05, 55.15))
UTM_EPSG = 32631
UTM_ZONE = 31
TARGET_DOL_M = 1.0
PLOUGH_START_UTC = "2026-10-18T06:00:00"
SURVEY_DATE = "2026-10-24"
RESURVEY_START_UTC = "2026-10-27T09:00:00"

_A = 6378137.0
_F = 1 / 298.257223563
_B = _A * (1 - _F)

# Where the seabed is hard: (KP from, KP to, DOL reached there in m)
HARD_GROUND = ((3.20, 3.55, 0.65), (7.85, 8.05, 0.85), (14.30, 14.42, 0.55), (21.00, 21.60, 0.90))

PLOUGH_HEADERS = ("Date Time", "KP", "DCC", "Latitude", "Longitude", "Easting", "Northing", "DOL", "Pitch",
                  "Roll", "Water Depth", "Tow Tension", "Cable Tension", "Speed", "Event")
PLOUGH_UNITS = ("", "km", "m", "deg", "deg", "m", "m", "m", "deg", "deg", "m", "kN", "kN", "m/s", "")


# --------------------------------------------------------------------------- geodesy
def geodesic_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """WGS 84 ellipsoidal distance (Vincenty's inverse; sub-millimetre at these lengths)."""
    return geodesic_inverse(lon1, lat1, lon2, lat2)[0]


def geodesic_inverse(lon1: float, lat1: float, lon2: float, lat2: float) -> Tuple[float, float]:
    """``(distance m, forward azimuth at the start, degrees)`` on WGS 84 (Vincenty's inverse)."""
    if lon1 == lon2 and lat1 == lat2:
        return 0.0, 0.0
    big_l = math.radians(lon2 - lon1)
    u1 = math.atan((1 - _F) * math.tan(math.radians(lat1)))
    u2 = math.atan((1 - _F) * math.tan(math.radians(lat2)))
    sin_u1, cos_u1, sin_u2, cos_u2 = math.sin(u1), math.cos(u1), math.sin(u2), math.cos(u2)
    lam = big_l
    for _ in range(200):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(cos_u2 * sin_lam, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam)
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos2_alpha = 1 - sin_alpha ** 2
        cos_2sm = cos_sigma - 2 * sin_u1 * sin_u2 / cos2_alpha if cos2_alpha else 0.0
        c = _F / 16 * cos2_alpha * (4 + _F * (4 - 3 * cos2_alpha))
        previous = lam
        lam = big_l + (1 - c) * _F * sin_alpha * (
            sigma + c * sin_sigma * (cos_2sm + c * cos_sigma * (-1 + 2 * cos_2sm ** 2)))
        if abs(lam - previous) < 1e-12:
            break
    u_sq = cos2_alpha * (_A ** 2 - _B ** 2) / _B ** 2
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    delta = big_b * sin_sigma * (cos_2sm + big_b / 4 * (
        cos_sigma * (-1 + 2 * cos_2sm ** 2) - big_b / 6 * cos_2sm * (-3 + 4 * sin_sigma ** 2) * (-3 + 4 * cos_2sm ** 2)))
    azimuth = math.degrees(math.atan2(cos_u2 * math.sin(lam), cos_u1 * sin_u2 - sin_u1 * cos_u2 * math.cos(lam)))
    return _B * big_a * (sigma - delta), azimuth


def geodesic_direct(lon, lat, azimuth, distance) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(lon, lat, forward azimuth)`` after ``distance`` m from each start along ``azimuth`` (Vincenty's direct)."""
    lon, lat, azimuth, distance = np.broadcast_arrays(*(np.asarray(v, dtype=float) for v in
                                                        (lon, lat, azimuth, distance)))
    alpha1 = np.radians(azimuth)
    sin_a1, cos_a1 = np.sin(alpha1), np.cos(alpha1)
    tan_u1 = (1 - _F) * np.tan(np.radians(lat))
    cos_u1 = 1 / np.sqrt(1 + tan_u1 ** 2)
    sin_u1 = tan_u1 * cos_u1
    sigma1 = np.arctan2(tan_u1, cos_a1)
    sin_alpha = cos_u1 * sin_a1
    cos2_alpha = 1 - sin_alpha ** 2
    u_sq = cos2_alpha * (_A ** 2 - _B ** 2) / _B ** 2
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    first = distance / (_B * big_a)
    sigma = first
    for _ in range(12):
        cos_2sm = np.cos(2 * sigma1 + sigma)
        sin_s, cos_s = np.sin(sigma), np.cos(sigma)
        delta = big_b * sin_s * (cos_2sm + big_b / 4 * (
            cos_s * (-1 + 2 * cos_2sm ** 2) - big_b / 6 * cos_2sm * (-3 + 4 * sin_s ** 2) * (-3 + 4 * cos_2sm ** 2)))
        sigma = first + delta
    cos_2sm = np.cos(2 * sigma1 + sigma)
    sin_s, cos_s = np.sin(sigma), np.cos(sigma)
    tmp = sin_u1 * sin_s - cos_u1 * cos_s * cos_a1
    lat2 = np.arctan2(sin_u1 * cos_s + cos_u1 * sin_s * cos_a1, (1 - _F) * np.sqrt(sin_alpha ** 2 + tmp ** 2))
    lam = np.arctan2(sin_s * sin_a1, cos_u1 * cos_s - sin_u1 * sin_s * cos_a1)
    c = _F / 16 * cos2_alpha * (4 + _F * (4 - 3 * cos2_alpha))
    big_l = lam - (1 - c) * _F * sin_alpha * (sigma + c * sin_s * (cos_2sm + c * cos_s * (-1 + 2 * cos_2sm ** 2)))
    return lon + np.degrees(big_l), np.degrees(lat2), np.degrees(np.arctan2(sin_alpha, -tmp))


def utm(lon: np.ndarray, lat: np.ndarray, zone: int = UTM_ZONE) -> Tuple[np.ndarray, np.ndarray]:
    """WGS 84 to UTM north (Krüger series to n^4: millimetre accuracy in the zone)."""
    n = _F / (2 - _F)
    big_a = _A / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
    alpha = (n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16 + 41 * n ** 4 / 180,
             13 * n ** 2 / 48 - 3 * n ** 3 / 5 + 557 * n ** 4 / 1440,
             61 * n ** 3 / 240 - 103 * n ** 4 / 140,
             49561 * n ** 4 / 161280)
    phi = np.radians(np.asarray(lat, dtype=float))
    lam = np.radians(np.asarray(lon, dtype=float)) - math.radians(zone * 6 - 183)
    root = 2 * math.sqrt(n) / (1 + n)
    t = np.sinh(np.arctanh(np.sin(phi)) - root * np.arctanh(root * np.sin(phi)))
    xi0 = np.arctan2(t, np.cos(lam))
    eta0 = np.arctanh(np.sin(lam) / np.sqrt(1 + t ** 2))
    xi, eta = xi0.copy(), eta0.copy()
    for j, a in enumerate(alpha, start=1):
        xi += a * np.sin(2 * j * xi0) * np.cosh(2 * j * eta0)
        eta += a * np.cos(2 * j * xi0) * np.sinh(2 * j * eta0)
    return 500000.0 + 0.9996 * big_a * eta, 0.9996 * big_a * xi


class SyntheticRoute:
    """The sample RPL: positions at KPs on the geodesic legs, offset across by DCC (+ starboard)."""

    def __init__(self, vertices: Sequence[Tuple[float, float]] = ROUTE_LONLAT):
        self.vertices = np.asarray(vertices, dtype=float)
        legs = [geodesic_inverse(*self.vertices[i], *self.vertices[i + 1]) for i in range(len(vertices) - 1)]
        self.segment_m = np.asarray([length for length, _azimuth in legs])
        self.azimuth = np.asarray([azimuth for _length, azimuth in legs])
        self.start_m = np.concatenate([[0.0], np.cumsum(self.segment_m)])

    @property
    def length_km(self) -> float:
        return float(self.start_m[-1] / 1000.0)

    def position(self, kp_km, dcc_m=0.0) -> Tuple[np.ndarray, np.ndarray]:
        s = np.clip(np.atleast_1d(np.asarray(kp_km, dtype=float)) * 1000.0, 0.0, self.start_m[-1])
        seg = np.clip(np.searchsorted(self.start_m, s, side="right") - 1, 0, len(self.segment_m) - 1)
        start = self.vertices[seg]
        lon, lat, heading = geodesic_direct(start[:, 0], start[:, 1], self.azimuth[seg], s - self.start_m[seg])
        dcc = np.broadcast_to(np.asarray(dcc_m, dtype=float), lon.shape)
        if np.any(dcc):
            # Starboard of increasing KP: 90 degrees clockwise from the heading, along a geodesic.
            lon, lat, _heading = geodesic_direct(lon, lat, heading + 90.0, dcc)
        return lon, lat


# --------------------------------------------------------------------------- the seabed and the cable
def dol_profile(kp_km) -> np.ndarray:
    """The DOL a plough reaches along the sample route (m, positive down), before noise."""
    kp = np.asarray(kp_km, dtype=float)
    dol = 1.55 + 0.35 * np.sin(kp / 1.9) + 0.15 * np.sin(kp / 0.43 + 1.0)
    for start, end, reached in HARD_GROUND:
        taper = 0.02
        inside = np.clip(np.minimum(kp - start, end - kp) / taper, 0.0, 1.0)
        weight = np.where(inside > 0, 0.5 - 0.5 * np.cos(np.pi * inside), 0.0)
        dol = dol * (1 - weight) + np.minimum(dol, reached) * weight
    return dol


def water_depth(kp_km) -> np.ndarray:
    kp = np.asarray(kp_km, dtype=float)
    return 42.0 + 18.0 * np.sin(kp / 6.5) + 4.0 * np.sin(kp / 1.1)


@dataclass
class PloughRun:
    """A whole plough run at a fixed rate (one row per record, in time order)."""
    time: np.ndarray        # UTC seconds
    kp: np.ndarray          # km
    dcc: np.ndarray
    lon: np.ndarray
    lat: np.ndarray
    easting: np.ndarray
    northing: np.ndarray
    dol: np.ndarray
    pitch: np.ndarray
    roll: np.ndarray
    water_depth: np.ndarray
    tow: np.ndarray
    cable: np.ndarray
    speed: np.ndarray
    event: List[str]


def _utc_seconds(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def plough_run(route: Optional[SyntheticRoute] = None, start_utc: str = PLOUGH_START_UTC, days: int = 3,
               rate_hz: float = 1.0, seed: int = 7) -> PloughRun:
    """The sample plough run: from ``start_utc`` to the end of the ``days``-th UTC day."""
    route = route or SyntheticRoute()
    rng = np.random.default_rng(seed)
    start = _utc_seconds(start_utc)
    first_midnight = math.floor(start / 86400.0) * 86400.0
    end = first_midnight + days * 86400.0
    dt = 1.0 / rate_hz
    t = np.arange(start, end, dt)
    elapsed = t - start
    speed = 0.115 + 0.045 * np.sin(2 * np.pi * elapsed / 5400.0) + 0.02 * np.sin(2 * np.pi * elapsed / 1300.0)
    events = [""] * t.size
    hours = elapsed / 3600.0
    for at, length in ((5.0, 0.5), (27.5, 0.75), (49.0, 0.35)):        # stops (h after start, h)
        stopped = (hours >= at) & (hours < at + length)
        speed[stopped] = 0.0
        idx = np.flatnonzero(stopped)
        if idx.size:
            events[idx[0]] = "STOP"
            if idx[-1] + 1 < t.size:
                events[idx[-1] + 1] = "RESUME"
    back = (hours >= 31.0) & (hours < 31.0 + 2.0 / 60.0)             # a short reverse
    speed[back] = -0.12
    if back.any():
        events[np.flatnonzero(back)[0]] = "REVERSE"
    kp = np.cumsum(np.r_[0.0, speed[:-1]] * dt) / 1000.0
    kp = np.minimum(kp, route.length_km - 0.05)
    dcc = 1.8 * np.sin(2 * np.pi * elapsed / 7200.0) + rng.normal(0.0, 0.25, t.size)
    lon, lat = route.position(kp, dcc)
    lon = lon + rng.normal(0.0, 1.5e-6, t.size)
    lat = lat + rng.normal(0.0, 1.0e-6, t.size)
    easting, northing = utm(lon, lat)
    dol = dol_profile(kp) + rng.normal(0.0, 0.03, t.size)
    moving = speed > 0
    tow = np.where(moving, 240.0 + 70.0 * dol + rng.normal(0.0, 12.0, t.size), 55.0 + rng.normal(0.0, 4.0, t.size))
    events[0] = "PLOUGH DOWN"
    events[-1] = "PLOUGH UP"
    return PloughRun(time=t, kp=kp, dcc=dcc, lon=lon, lat=lat, easting=easting, northing=northing, dol=dol,
                     pitch=rng.normal(0.2, 0.8, t.size), roll=rng.normal(0.0, 1.2, t.size),
                     water_depth=water_depth(kp) + rng.normal(0.0, 0.08, t.size),
                     tow=tow, cable=8.0 + rng.normal(0.0, 0.8, t.size), speed=speed, event=events)


# --------------------------------------------------------------------------- writing deliveries
def _fmt(values: np.ndarray, decimals: int) -> List[str]:
    return [f"{v:.{decimals}f}" for v in values]


def _stamp(seconds: np.ndarray) -> List[str]:
    return [datetime.fromtimestamp(float(s), timezone.utc).strftime("%Y-%m-%d %H:%M:%S") for s in seconds]


def _csv(headers: Sequence[str], units: Sequence[str], columns: Sequence[Sequence[str]], sep: str = ",") -> bytes:
    lines = [sep.join(headers)]
    if units:
        lines.append(sep.join(units))
    lines.extend(sep.join(row) for row in zip(*columns))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _plough_columns(run: PloughRun, rows: np.ndarray, dol_offset: Optional[np.ndarray] = None) -> List[List[str]]:
    dol = run.dol[rows] if dol_offset is None else run.dol[rows] + dol_offset
    return [_stamp(run.time[rows]), _fmt(run.kp[rows], 4), _fmt(run.dcc[rows], 2), _fmt(run.lat[rows], 7),
            _fmt(run.lon[rows], 7), _fmt(run.easting[rows], 2), _fmt(run.northing[rows], 2), _fmt(dol, 2),
            _fmt(run.pitch[rows], 1), _fmt(run.roll[rows], 1), _fmt(run.water_depth[rows], 2),
            _fmt(run.tow[rows], 1), _fmt(run.cable[rows], 1), _fmt(run.speed[rows], 3),
            [run.event[i] for i in rows]]


def _day_rows(run: PloughRun, day_start: float, overlap_min: float) -> np.ndarray:
    keep = (run.time >= day_start) & (run.time < day_start + 86400.0 + overlap_min * 60.0)
    # A 30-minute logging gap 8 hours into the run (the plough kept moving).
    gap_start = run.time[0] + 8 * 3600.0
    gap = (run.time >= gap_start) & (run.time < gap_start + 1800.0)
    return np.flatnonzero(keep & ~gap)


def day_name(day_start: float) -> str:
    return datetime.fromtimestamp(day_start, timezone.utc).strftime("PLG_%Y%m%d.csv")


def _damage_day(columns: List[List[str]]) -> List[List[str]]:
    """Day 2's bad cells: three 'n/a' DOLs (missing), two '#VALUE!' tensions, one line cut short."""
    n = len(columns[0])
    for i in (n // 7, n // 7 + 1, n // 7 + 2):
        columns[7][i] = "n/a"
    for i in (n // 3, 2 * n // 3):
        columns[11][i] = "#VALUE!"
    return columns


def daily_files(run: PloughRun, overlap_min: float = 10.0) -> List[Tuple[str, bytes]]:
    """``[(name, bytes)]``: one file per UTC day, each running ``overlap_min`` past midnight."""
    files = []
    first = math.floor(run.time[0] / 86400.0) * 86400.0
    day = 0
    while first + day * 86400.0 <= run.time[-1]:
        start = first + day * 86400.0
        rows = _day_rows(run, start, overlap_min)
        if rows.size:
            columns = _plough_columns(run, rows)
            if day == 1:
                columns = _damage_day(columns)
            body = _csv(PLOUGH_HEADERS, PLOUGH_UNITS, columns)
            if day == 1:
                lines = body.split(b"\r\n")
                cut = 2 + len(rows) // 2
                lines[cut] = b",".join(lines[cut].split(b",")[:9])     # the line cut short
                body = b"\r\n".join(lines)
            files.append((day_name(start), body))
        day += 1
    return files


def redelivered_day(run: PloughRun, day_index: int = 1, overlap_min: float = 10.0) -> Tuple[str, bytes]:
    """Day ``day_index`` re-sent under the same name: DOL +0.05 m from 10:00 to 12:00, bad cells fixed."""
    first = math.floor(run.time[0] / 86400.0) * 86400.0
    start = first + day_index * 86400.0
    rows = _day_rows(run, start, overlap_min)
    window = (run.time[rows] >= start + 10 * 3600.0) & (run.time[rows] < start + 12 * 3600.0)
    columns = _plough_columns(run, rows, np.where(window, 0.05, 0.0))
    return day_name(start), _csv(PLOUGH_HEADERS, PLOUGH_UNITS, columns)


def survey_listing(route: Optional[SyntheticRoute] = None, kp_from: float = 0.0, kp_to: float = 12.0, step_m: float = 1.0,
                   seed: int = 11, name: str = "PLS01_burial_listing.csv") -> Tuple[str, bytes]:
    """A post-lay survey listing at ``step_m`` (acquired on :data:`SURVEY_DATE`; no time column)."""
    route = route or SyntheticRoute()
    rng = np.random.default_rng(seed)
    kp = np.round(np.arange(kp_from * 1000.0, kp_to * 1000.0 + 1e-6, step_m) / 1000.0, 6)
    dcc = 1.2 * np.sin(kp / 0.7) + rng.normal(0.0, 0.15, kp.size)
    lon, lat = route.position(kp, dcc)
    easting, northing = utm(lon, lat)
    dol = dol_profile(kp) + 0.08 + rng.normal(0.0, 0.04, kp.size)     # some settlement since ploughing
    comments = [""] * kp.size
    comments[0] = "Start of survey"
    comments[-1] = "End of survey"
    columns = [_fmt(kp, 3), _fmt(easting, 2), _fmt(northing, 2), _fmt(dcc, 2),
               _fmt(water_depth(kp) + rng.normal(0.0, 0.05, kp.size), 2), _fmt(dol, 2), comments]
    return name, _csv(("KP", "Easting", "Northing", "DCC", "Water Depth", "DOL", "Comment"), (), columns)


def _dm(values: np.ndarray, positive: str, negative: str) -> List[str]:
    out = []
    for value in values:
        hemi = positive if value >= 0 else negative
        degrees = int(abs(value))
        minutes = (abs(value) - degrees) * 60.0
        out.append(f"{degrees:02d}°{minutes:07.4f}'{hemi}".replace(".", ","))
    return out


def rov_resurvey(route: Optional[SyntheticRoute] = None, kp_from: float = 3.0, kp_to: float = 3.8, step_m: float = 0.5,
                 start_utc: str = RESURVEY_START_UTC, seed: int = 13,
                 name: str = "ROV_resurvey_KP3.txt") -> Tuple[str, bytes]:
    """A short ROV re-survey in another house style (see the module notes)."""
    route = route or SyntheticRoute()
    rng = np.random.default_rng(seed)
    kp = np.arange(kp_from * 1000.0, kp_to * 1000.0 + 1e-6, step_m) / 1000.0
    seconds = _utc_seconds(start_utc) + np.arange(kp.size) * 2.0
    dcc = rng.normal(0.0, 0.3, kp.size)
    lon, lat = route.position(kp, dcc)
    dol_cm = (dol_profile(kp) + 0.25 + rng.normal(0.0, 0.03, kp.size)) * 100.0   # after remedial jetting
    stamps = [datetime.fromtimestamp(float(s), timezone.utc) for s in seconds]
    columns = [[f"{v:.1f}".replace(".", ",") for v in kp * 1000.0],
               [s.strftime("%d.%m.%Y") for s in stamps], [s.strftime("%H:%M:%S") for s in stamps],
               _dm(lat, "N", "S"), _dm(lon, "E", "W"),
               [f"{v:.2f}".replace(".", ",") for v in water_depth(kp)],
               [f"{v:.0f}" for v in dol_cm], [""] * kp.size]
    return name, _csv(("KP [m]", "Date", "Time", "Lat", "Lon", "WD [m]", "DOL [cm]", "Remarks"), (), columns,
                      sep=";")


def examples(rate_hz: float = 1.0) -> Dict[str, Tuple[str, bytes]]:
    """Every sample delivery, keyed ``day1``…, ``day2_redelivered``, ``survey``, ``resurvey``."""
    route = SyntheticRoute()
    run = plough_run(route, rate_hz=rate_hz)
    out = {f"day{i + 1}": f for i, f in enumerate(daily_files(run))}
    out["day2_redelivered"] = redelivered_day(run)
    out["survey"] = survey_listing(route)
    out["resurvey"] = rov_resurvey(route)
    return out
