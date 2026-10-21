# -*- coding: utf-8 -*-
"""Check a dataset's delivered KPs against its positions on the RPL they refer to.

Records that carry positions (latitude/longitude, or easting/northing in the
mapping's CRS) are placed on the dataset's RPL with the plugin's KP
(:meth:`...kp_geo_utils.RouteFrame.kp_at_point`) and the delivered KP is
compared with it. Up to :data:`SAMPLE` records, spread along the data, are
checked. The result says how well they agree (within 1 m, 1–5 m, more: the
same thresholds as the RPL check), how far the positions lie off the route,
and two common causes of disagreement: a constant shift (KPs from another
start KP) and a difference growing along the route (KPs measured on a
projection, i.e. grid chainage, or on another route).

Nothing is changed: the delivered KPs stay as delivered. The summary is
recorded in the data file's history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

from ...kp_datum import AGREE_M, WARN_M
from . import roles as R

SAMPLE = 2000


@dataclass
class KpVerification:
    compared: int = 0
    median_diff_m: float = 0.0          # delivered - measured
    max_abs_diff_m: float = 0.0
    worst_kp: Optional[float] = None
    dcc_median_m: float = 0.0
    dcc_max_m: float = 0.0
    shift_m: Optional[float] = None     # a constant offset explains the differences
    scale_ppm: Optional[float] = None   # the difference grows along the route
    route_label: str = ""
    message: str = ""

    @property
    def level(self) -> str:
        if self.compared < 2 or self.max_abs_diff_m < AGREE_M:
            return "ok"
        return "warn" if self.max_abs_diff_m >= WARN_M else "info"

    def summary(self) -> str:
        if self.message:
            return self.message
        on = f" on {self.route_label}" if self.route_label else ""
        head = (f"{self.compared:,} records checked{on}: delivered KPs "
                + (f"agree within {AGREE_M:g} m" if self.level == "ok" else
                   f"differ by up to {self.max_abs_diff_m:,.1f} m (at KP {self.worst_kp:.3f})"))
        parts = [head, f"median difference {self.median_diff_m:+.2f} m",
                 f"positions {self.dcc_median_m:.1f} m off the route (median; at most {self.dcc_max_m:.1f} m)"]
        if self.shift_m is not None:
            parts.append(f"the KPs look shifted by {self.shift_m:+.1f} m: is the start KP different?")
        if self.scale_ppm is not None:
            parts.append(f"the difference grows by {self.scale_ppm / 1000.0:+.2f} m per km: the KPs may be measured "
                         "on a projection (grid chainage) or on another route")
        return "; ".join(parts) + "."


def sample_rows(n: int, count: int = SAMPLE) -> np.ndarray:
    if n <= count:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, count).round().astype(int))


def analyse(delivered_km: np.ndarray, measured_km: np.ndarray, dcc_m: np.ndarray,
            route_label: str = "") -> KpVerification:
    """The verdict from delivered and measured KPs (pure; ``measured`` NaN where a position failed)."""
    ok = np.isfinite(delivered_km) & np.isfinite(measured_km)
    result = KpVerification(route_label=route_label)
    if ok.sum() < 2:
        result.message = "Too few records with both a KP and a position to check."
        return result
    delivered, measured, dcc = delivered_km[ok], measured_km[ok], np.abs(dcc_m[ok])
    diff = (delivered - measured) * 1000.0
    k = int(np.argmax(np.abs(diff)))
    result.compared = int(ok.sum())
    result.median_diff_m = float(np.median(diff))
    result.max_abs_diff_m = float(np.abs(diff[k]))
    result.worst_kp = float(delivered[k])
    result.dcc_median_m = float(np.median(dcc))
    result.dcc_max_m = float(np.max(dcc))
    if result.level != "ok":
        spread = float(np.percentile(diff, 95) - np.percentile(diff, 5))
        if abs(result.median_diff_m) >= AGREE_M and spread < max(AGREE_M, abs(result.median_diff_m) / 10.0):
            result.shift_m = result.median_diff_m
        elif np.ptp(measured) > 0.5:
            slope, _intercept = np.polyfit(measured, diff, 1)        # m of difference per km
            fitted = np.abs(diff - np.polyval([slope, _intercept], measured))
            if abs(slope) >= 0.05 and float(np.percentile(fitted, 95)) < max(AGREE_M, result.max_abs_diff_m / 5.0):
                result.scale_ppm = float(slope * 1000.0)
    return result


def positions_wgs84(columns: Dict, profile: Optional[Dict]):
    """``(lon, lat)`` arrays from latitude/longitude, or easting/northing in the mapping's CRS; None if neither."""
    lon, lat = columns.get(R.LON), columns.get(R.LAT)
    if lon is not None and lat is not None and np.isfinite(lon).any():
        return np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)
    east, north = columns.get(R.EASTING), columns.get(R.NORTHING)
    crs_text = (profile or {}).get("crs") or ""
    if east is None or north is None or not crs_text:
        return None
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
    crs = QgsCoordinateReferenceSystem(crs_text)
    if not crs.isValid():
        return None
    transform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                       QgsProject.instance().transformContext())
    out_lon = np.full(len(east), np.nan)
    out_lat = np.full(len(east), np.nan)
    for i, (x, y) in enumerate(zip(east, north)):
        if np.isfinite(x) and np.isfinite(y):
            try:
                point = transform.transform(QgsPointXY(float(x), float(y)))
            except Exception:
                continue
            out_lon[i], out_lat[i] = point.x(), point.y()
    return out_lon, out_lat


def verify(route, kp_km: np.ndarray, lon: np.ndarray, lat: np.ndarray, route_label: str = "") -> KpVerification:
    """Measure each sampled record's KP on ``route`` (a WGS84 RouteFrame) and compare."""
    from qgis.core import QgsPointXY
    rows = sample_rows(len(kp_km))
    rows = rows[np.isfinite(lon[rows]) & np.isfinite(lat[rows]) & np.isfinite(kp_km[rows])]
    measured = np.full(rows.size, np.nan)
    dcc = np.full(rows.size, np.nan)
    for j, i in enumerate(rows):
        hit = route.kp_at_point(QgsPointXY(float(lon[i]), float(lat[i])))
        if hit.snapped_xy is not None:
            measured[j], dcc[j] = hit.kp_km, hit.dcc_m
    return analyse(np.asarray(kp_km, dtype=float)[rows], measured, dcc, route_label)


def dataset_route(model, dataset: Dict):
    """``(RouteFrame, label)`` of the RPL a dataset's KPs refer to. Raises ValueError."""
    ref = str(dataset.get("rpl_id") or "")
    plan_rpl = str(getattr(model, "resolved_rpl_id", "") or "")
    if not ref or ref == plan_rpl:
        if getattr(model, "route", None) is None:
            raise ValueError("The plan has no route: set its RPL on the Inputs tab.")
        return model.route, dataset.get("rpl_label") or "this plan's RPL"
    from qgis.core import QgsProject
    from ..rereference_qgis import rpl_label, rpl_route
    store = getattr(model, "workbench_store", None)
    rpl = store.get_rpl(ref) if store is not None else None
    if rpl is None:
        raise ValueError(f"{dataset.get('rpl_label') or 'The RPL'} is not in the Workbench.")
    route, _distance, _positions = rpl_route(store, rpl, QgsProject.instance(), model.kp_mode())
    return route, rpl_label(rpl)


def check_dataset(store, model, dataset: Dict) -> KpVerification:
    """Verify a dataset's delivered KPs (main thread; at most :data:`SAMPLE` position lookups)."""
    route, label = dataset_route(model, dataset)
    data = store.effective(dataset["dataset_id"], [R.LAT, R.LON, R.EASTING, R.NORTHING])
    kp = np.asarray(data[R.KP], dtype=float)
    rows = sample_rows(kp.size)
    sampled = {key: np.asarray(data[key], dtype=float)[rows] for key in (R.LAT, R.LON, R.EASTING, R.NORTHING)}
    profile_row = store.get_profile(dataset["profile_id"]) if dataset.get("profile_id") else None
    positions = positions_wgs84(sampled, profile_row["profile"] if profile_row else None)
    if positions is None:
        return KpVerification(route_label=label, message="This dataset has no positions (latitude/longitude, or "
                                                         "easting/northing with a CRS) to check its KPs against.")
    return verify(route, kp[rows], positions[0], positions[1], label)
