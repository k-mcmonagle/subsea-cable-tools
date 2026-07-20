# -*- coding: utf-8 -*-
"""UI-free geometry helpers for the Explorer's Processing tab.

Turns a :class:`LayDataset` (as-laid points) into:

* an **interval listing** — stations every N metres along the as-laid path with
  KP and sampled numeric attributes, and
* an **as-laid RPL** — a Douglas-Peucker simplification of the path whose
  vertices become route-position-list alter-course points, emitted in the same
  Points + Lines schema as the plugin's imported RPLs.

Distances and bearings are geodesic (via :class:`QgsDistanceArea` on WGS84).
Everything here is pure data in / QGIS-geometry out, so it can be unit-tested
without the Explorer UI.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)

from ..processing.cable_lay_parsers import WKT_KEY, fields_from_specs

# RPL schema mirrors processing/import_excel_rpl_algorithm.py output layers.
POINT_RPL_SPECS: List[Tuple[str, str]] = [
    ("PosNo", "int"),
    ("Event", "str"),
    ("DistCumulative", "float"),
    ("CableDistCumulative", "float"),
    ("ApproxDepth", "float"),
    ("Remarks", "str"),
    ("ChartNo", "int"),
    ("Latitude", "float"),
    ("Longitude", "float"),
    ("SourceFile", "str"),
]

LINE_RPL_SPECS: List[Tuple[str, str]] = [
    ("FromPos", "int"),
    ("ToPos", "int"),
    ("Bearing", "float"),
    ("DistBetweenPos", "float"),
    ("Slack", "float"),
    ("CableDistBetweenPos", "float"),
    ("CableCode", "str"),
    ("FiberPair", "str"),
    ("CableType", "str"),
    ("LayDirection", "str"),
    ("LayVessel", "str"),
    ("ProtectionMethod", "str"),
    ("DateInstalled", "str"),
    ("TargetBurialDepth", "float"),
    ("BurialDepth", "float"),
    ("TerritorialWater", "str"),
    ("EEZ", "str"),
    ("SourceFile", "str"),
]

_DEPTH_KEYWORDS = ("depth", "water_depth", "wd")


# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------
def path_order(dataset) -> List[int]:
    """Row indices in path order: by time when available, else record order."""
    n = dataset.row_count
    times = dataset.time_epoch
    if times is None:
        return list(range(n))
    tt = np.asarray(times, dtype=float)
    keys = np.where(np.isfinite(tt), tt, np.inf)
    return np.argsort(keys, kind="stable").tolist()


def path_points(dataset) -> List[Tuple[float, float, int]]:
    """Ordered ``(lon, lat, source_row)`` tuples with finite geometry only."""
    if not dataset.has_geometry:
        return []
    lon = dataset.lon
    lat = dataset.lat
    points: List[Tuple[float, float, int]] = []
    for row in path_order(dataset):
        x = lon[row]
        y = lat[row]
        if np.isfinite(x) and np.isfinite(y):
            points.append((float(x), float(y), int(row)))
    return points


def cumulative_m(points, distance) -> List[float]:
    """Cumulative geodesic distance (m) at each path vertex."""
    cum = [0.0]
    for i in range(1, len(points)):
        p0 = QgsPointXY(points[i - 1][0], points[i - 1][1])
        p1 = QgsPointXY(points[i][0], points[i][1])
        try:
            step = distance.measureLine(p0, p1)
        except Exception:
            step = 0.0
        cum.append(cum[-1] + step)
    return cum


def depth_field(dataset) -> Optional[str]:
    for name in dataset.field_names:
        if not dataset.is_numeric_field(name):
            continue
        if any(key in name.lower() for key in _DEPTH_KEYWORDS):
            return name
    return None


# ---------------------------------------------------------------------------
# Interval listing
# ---------------------------------------------------------------------------
def interval_listing(dataset, interval_m: float, distance, fields: List[str]) -> List[dict]:
    """Stations every ``interval_m`` metres along the as-laid path."""
    points = path_points(dataset)
    if len(points) < 2 or interval_m <= 0:
        return []
    cum = cumulative_m(points, distance)
    total = cum[-1]
    caches = {f: dataset.numeric(f) for f in fields if f in dataset.columns}

    stations: List[dict] = []
    d = 0.0
    seg = 0
    pos = 0
    while d <= total + 1e-6:
        while seg < len(points) - 2 and cum[seg + 1] < d:
            seg += 1
        seg_len = cum[seg + 1] - cum[seg]
        ratio = 0.0 if seg_len <= 0 else (d - cum[seg]) / seg_len
        ratio = min(max(ratio, 0.0), 1.0)
        lon = points[seg][0] + ratio * (points[seg + 1][0] - points[seg][0])
        lat = points[seg][1] + ratio * (points[seg + 1][1] - points[seg][1])
        nearest_row = points[seg][2] if ratio < 0.5 else points[seg + 1][2]
        record = {
            "PosNo": pos,
            "KP_km": d / 1000.0,
            "Lat_dd": lat,
            "Lon_dd": lon,
        }
        for name, arr in caches.items():
            value = arr[nearest_row]
            record[name] = float(value) if np.isfinite(value) else None
        record[WKT_KEY] = f"POINT({lon} {lat})"
        stations.append(record)
        pos += 1
        d += interval_m
    return stations


def listing_specs(fields: List[str]) -> List[Tuple[str, str]]:
    specs = [("PosNo", "int"), ("KP_km", "float"), ("Lat_dd", "float"), ("Lon_dd", "float")]
    specs.extend((name, "float") for name in fields)
    return specs


# ---------------------------------------------------------------------------
# Douglas-Peucker simplification (metric)
# ---------------------------------------------------------------------------
def simplify_indices(points, tolerance_m: float) -> List[int]:
    """Indices of the vertices kept by Douglas-Peucker at ``tolerance_m``."""
    n = len(points)
    if n < 3 or tolerance_m <= 0:
        return list(range(n))
    mean_lat = sum(p[1] for p in points) / n
    k = math.cos(math.radians(mean_lat))
    xs = np.array([p[0] for p in points]) * 111320.0 * k
    ys = np.array([p[1] for p in points]) * 110540.0

    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        x0, y0 = xs[i0], ys[i0]
        x1, y1 = xs[i1], ys[i1]
        dx = x1 - x0
        dy = y1 - y0
        seg_len2 = dx * dx + dy * dy
        idx = np.arange(i0 + 1, i1)
        if seg_len2 == 0:
            dist = np.hypot(xs[idx] - x0, ys[idx] - y0)
        else:
            t = ((xs[idx] - x0) * dx + (ys[idx] - y0) * dy) / seg_len2
            proj_x = x0 + t * dx
            proj_y = y0 + t * dy
            dist = np.hypot(xs[idx] - proj_x, ys[idx] - proj_y)
        if dist.size == 0:
            continue
        worst = int(np.argmax(dist))
        if dist[worst] > tolerance_m:
            split = i0 + 1 + worst
            keep[split] = True
            stack.append((i0, split))
            stack.append((split, i1))
    return [i for i in range(n) if keep[i]]


def _bearing(distance, p0: QgsPointXY, p1: QgsPointXY) -> Optional[float]:
    try:
        return math.degrees(distance.bearing(p0, p1)) % 360.0
    except Exception:
        try:
            return p0.azimuth(p1) % 360.0
        except Exception:
            return None


# ---------------------------------------------------------------------------
# As-laid RPL build
# ---------------------------------------------------------------------------
def build_aslaid_rpl(dataset, tolerance_m: float, distance, source_name: str):
    """Return ``(point_rows, line_rows)`` for the fitted as-laid RPL."""
    points = path_points(dataset)
    if len(points) < 2:
        return [], []
    keep = simplify_indices([(p[0], p[1]) for p in points], tolerance_m)
    cum = cumulative_m(points, distance)
    dfield = depth_field(dataset)
    depths = dataset.numeric(dfield) if dfield else None

    point_rows: List[dict] = []
    for pos, i in enumerate(keep, start=1):
        lon, lat, row = points[i]
        depth = None
        if depths is not None and np.isfinite(depths[row]):
            depth = float(depths[row])
        if pos == 1:
            event = "START"
        elif pos == len(keep):
            event = "END"
        else:
            event = "ALTER"
        point_rows.append({
            "PosNo": pos,
            "Event": event,
            "DistCumulative": cum[i],
            "CableDistCumulative": cum[i],
            "ApproxDepth": depth,
            "Remarks": "",
            "ChartNo": None,
            "Latitude": lat,
            "Longitude": lon,
            "SourceFile": source_name,
            WKT_KEY: f"POINT({lon} {lat})",
        })

    line_rows: List[dict] = []
    for pos in range(len(keep) - 1):
        i0 = keep[pos]
        i1 = keep[pos + 1]
        lon0, lat0, _ = points[i0]
        lon1, lat1, _ = points[i1]
        seg_d = cum[i1] - cum[i0]
        bearing = _bearing(distance, QgsPointXY(lon0, lat0), QgsPointXY(lon1, lat1))
        line_rows.append({
            "FromPos": pos + 1,
            "ToPos": pos + 2,
            "Bearing": bearing,
            "DistBetweenPos": seg_d,
            "Slack": None,
            "CableDistBetweenPos": seg_d,
            "CableCode": "",
            "FiberPair": "",
            "CableType": "",
            "LayDirection": "",
            "LayVessel": "",
            "ProtectionMethod": "",
            "DateInstalled": "",
            "TargetBurialDepth": None,
            "BurialDepth": None,
            "TerritorialWater": "",
            "EEZ": "",
            "SourceFile": source_name,
            WKT_KEY: f"LINESTRING({lon0} {lat0}, {lon1} {lat1})",
        })
    return point_rows, line_rows


# ---------------------------------------------------------------------------
# Memory layer construction
# ---------------------------------------------------------------------------
def build_memory_layer(name: str, geom_type: str, specs, rows) -> QgsVectorLayer:
    """Build an in-memory WGS84 layer from ``specs`` and ``rows`` (with WKT)."""
    layer = QgsVectorLayer(f"{geom_type}?crs=EPSG:4326", name, "memory")
    provider = layer.dataProvider()
    fields = fields_from_specs(specs)
    provider.addAttributes(fields.toList())
    layer.updateFields()

    features = []
    for row in rows:
        feature = QgsFeature(layer.fields())
        wkt = row.get(WKT_KEY)
        if wkt:
            feature.setGeometry(QgsGeometry.fromWkt(wkt))
        feature.setAttributes([row.get(name) for name, _type in specs])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer
