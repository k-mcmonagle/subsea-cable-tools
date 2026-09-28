"""The plugin's KP datum: measured chainage, anchored at the RPL's start KP.

One rule everywhere: **KP is measured by us** — geodesic chainage on WGS84
along the route line (``kp_geo_utils.RouteFrame`` with
``kp_range_utils.make_distance_area``). An RPL contributes only its **start
KP** (the stated KP of its first position; normally 0), so a route that
starts at KP 12.345 reads 12.345 at its first vertex.

The KPs printed in the RPL (``DistCumulative``) are never used for
positions. They are *checked* in the background: :func:`compare_stated_kps`
reports how far they differ from our measurement and whether they look like
grid (cartesian) distances, so the user is told once, plainly, when the
document and the plugin disagree by more than a metre or so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# Differences below this are measurement noise (3 dp display = 1 m).
AGREE_M = 1.0
# At or above this the user is warned (the user's "multiple metres").
WARN_M = 5.0


@dataclass
class KpCheck:
    """How an RPL's stated KPs compare with the measured KPs."""
    positions: int = 0
    compared: int = 0
    start_kp_km: float = 0.0
    max_diff_m: float = 0.0          # |stated - measured|, worst position
    worst_kp_km: Optional[float] = None
    end_diff_m: float = 0.0          # signed stated - measured at the last position
    grid_max_diff_m: Optional[float] = None   # same, vs planar (UTM) chainage
    grid_zone: str = ""
    messages: List[str] = field(default_factory=list)

    @property
    def level(self) -> str:
        """``ok`` / ``info`` / ``warn`` for the UI."""
        if self.compared < 2 or self.max_diff_m < AGREE_M:
            return "ok"
        return "warn" if self.max_diff_m >= WARN_M else "info"

    @property
    def looks_grid(self) -> bool:
        """Stated KPs match planar grid distances far better than geodesic."""
        return (self.grid_max_diff_m is not None and self.max_diff_m >= AGREE_M
                and self.grid_max_diff_m < max(AGREE_M, self.max_diff_m / 5.0))

    def summary(self) -> str:
        if self.compared < 2:
            return "The RPL has no stated KPs to check."
        if self.level == "ok":
            return (f"RPL stated KPs agree with the measured KPs (within "
                    f"{self.max_diff_m:.2f} m over {self.compared} positions).")
        text = (f"RPL stated KPs differ from the measured KPs by up to "
                f"{self.max_diff_m:.1f} m (at KP {self.worst_kp_km:.3f}; "
                f"{self.end_diff_m:+.1f} m at the route end). ")
        if self.looks_grid:
            text += (f"They match grid (cartesian, {self.grid_zone}) distances "
                     f"to within {self.grid_max_diff_m:.1f} m, so the RPL was "
                     "probably chained on the map projection. ")
        text += ("The plugin measures KP geodesically (WGS84) from the RPL start "
                 "KP; the stated values are shown for reference only.")
        return text


def compare_stated_kps(measured_km: Sequence[Optional[float]],
                       stated_km: Sequence[Optional[float]],
                       grid_km: Optional[Sequence[Optional[float]]] = None,
                       grid_zone: str = "") -> KpCheck:
    """Compare per-position KPs (pure). Lists are parallel, route order.

    ``measured_km`` are our KPs at each RPL position (already anchored at
    the start KP); ``grid_km`` optionally the same chainage measured
    planar in a projected CRS (anchored the same way).
    """
    check = KpCheck(positions=len(measured_km), grid_zone=grid_zone)
    pairs = [(i, m, s) for i, (m, s) in enumerate(zip(measured_km, stated_km))
             if m is not None and s is not None]
    check.compared = len(pairs)
    if not pairs:
        return check
    check.start_kp_km = float(pairs[0][2])
    worst = 0.0
    for i, m, s in pairs:
        diff = abs(float(s) - float(m)) * 1000.0
        if diff > worst:
            worst, check.worst_kp_km = diff, float(m)
    check.max_diff_m = worst
    check.end_diff_m = (float(pairs[-1][2]) - float(pairs[-1][1])) * 1000.0
    if grid_km is not None:
        grid_pairs = [(g, s) for g, s in zip(grid_km, stated_km)
                      if g is not None and s is not None]
        if grid_pairs:
            check.grid_max_diff_m = max(abs(float(s) - float(g)) * 1000.0
                                        for g, s in grid_pairs)
    return check


def utm_epsg_for(lon: float, lat: float) -> int:
    """WGS84 / UTM zone EPSG code for a position."""
    zone = int((float(lon) + 180.0) // 6.0) % 60 + 1
    return (32600 if float(lat) >= 0 else 32700) + zone


# ---------------------------------------------------------------- QGIS side
def rpl_positions(points_layer) -> List[Tuple[object, Optional[float]]]:
    """``[(QgsPointXY in WGS84, stated KP km or None)]`` in SeqNo order."""
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                           QgsPointXY, QgsProject)
    if points_layer is None or not points_layer.isValid():
        return []
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    xform = None
    if points_layer.crs() != wgs84:
        xform = QgsCoordinateTransform(points_layer.crs(), wgs84, QgsProject.instance())
    names = set(points_layer.fields().names())
    rows = []
    for index, feat in enumerate(points_layer.getFeatures()):
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        point = QgsPointXY(geom.asPoint())
        if xform is not None:
            try:
                point = xform.transform(point)
            except Exception:
                continue
        seq = index
        if "SeqNo" in names:
            try:
                seq = int(feat["SeqNo"])
            except (TypeError, ValueError):
                pass
        stated = None
        if "DistCumulative" in names:
            try:
                stated = float(feat["DistCumulative"])
            except (TypeError, ValueError):
                stated = None
        rows.append((seq, index, point, stated))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(point, stated) for _seq, _i, point, stated in rows]


def start_kp_of(positions: Sequence[Tuple[object, Optional[float]]]) -> float:
    """The RPL's start KP: stated KP of its first position (0 if absent)."""
    if not positions:
        return 0.0
    stated = positions[0][1]
    try:
        return float(stated) if stated is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def check_route_kps(route, positions) -> KpCheck:
    """Compare an RPL's stated KPs with ``route`` (a WGS84 RouteFrame).

    Also measures planar chainage in the route's UTM zone so a document
    chained on the projection is recognised rather than reported as error.
    """
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                           QgsProject)
    if route is None or not positions:
        return KpCheck()
    measured = []
    for point, _stated in positions:
        try:
            hit = route.kp_at_point(point)
            measured.append(hit.kp_km if hit.snapped_xy is not None else None)
        except Exception:
            measured.append(None)
    stated = [s for _p, s in positions]
    grid, zone = None, ""
    try:
        mid = positions[len(positions) // 2][0]
        epsg = utm_epsg_for(mid.x(), mid.y())
        zone = f"UTM EPSG:{epsg}"
        xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                       QgsCoordinateReferenceSystem(f"EPSG:{epsg}"),
                                       QgsProject.instance())
        projected = [xform.transform(p) for p, _s in positions]
        start = start_kp_of(positions)
        grid, running = [start], 0.0
        for a, b in zip(projected, projected[1:]):
            running += ((b.x() - a.x()) ** 2 + (b.y() - a.y()) ** 2) ** 0.5
            grid.append(start + running / 1000.0)
    except Exception:
        grid, zone = None, ""
    return compare_stated_kps(measured, stated, grid, zone)
