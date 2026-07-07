# -*- coding: utf-8 -*-
"""WorkbenchV3Adapter — the contract the Cable Lay Simulator (catenary V3)
consumes from the workbench.

Gives the simulator route context instead of a canvas-centred sandbox:

- list_rpls()                       registered routes to pick from
- bathymetry_profile(...)           along-route depth profile around a KP
- lay_azimuth_deg(...)              forward route bearing at a KP
- assembly_window(...)              V2/V3-key-compatible assembly JSON slice
                                    around the cable distance at a KP
                                    (requires a stored wb_fit for the RPL)
- push_results(...)                 KP-referenced result layer back into the
                                    workbench GeoPackage

Assembly dicts use the exact V2 keys (type/name/length_m/q_water_npm/...)
plus diameter_m / cd_normal / cd_tangential, so
catenary.v3.ui.solve_controller.build_assembly consumes them unmodified.

Wiring this into the V3 dialog is deliberately out of scope here; the class
is self-contained so the dialog can adopt it in a later release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsWkbTypes

from ..kp_range_utils import make_distance_area
from . import assembly_model as am
from . import rpl_engine, schema
from .depth_service import DepthService, DepthSourceConfig
from .rpl_layer_io import RplLayerSync
from .store import WorkbenchStore

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


@dataclass
class RplRef:
    rpl_id: str
    name: str
    length_km: float
    kind: str


class WorkbenchV3Adapter:
    def __init__(self, store: WorkbenchStore, project: Optional[QgsProject] = None):
        self.store = store
        self.project = project or QgsProject.instance()
        self.da = make_distance_area(WGS84, self.project.transformContext())
        self._model_cache: Dict[str, rpl_engine.RplModel] = {}

    # ------------------------------------------------------------- routes --
    def list_rpls(self) -> List[RplRef]:
        refs = []
        for row in self.store.list_rpls():
            model = self._model(row.get("rpl_id"))
            length = model.total_route_km() if model else 0.0
            refs.append(RplRef(
                rpl_id=row.get("rpl_id") or "",
                name=row.get("name") or "",
                length_km=length,
                kind=row.get("kind") or "",
            ))
        return refs

    def _model(self, rpl_id: Optional[str]) -> Optional[rpl_engine.RplModel]:
        if not rpl_id:
            return None
        if rpl_id in self._model_cache:
            return self._model_cache[rpl_id]
        row = self.store.get_rpl(rpl_id)
        if not row:
            return None
        points_layer = self.store.open_layer(row.get("points_layer"))
        lines_layer = self.store.open_layer(row.get("lines_layer"))
        if points_layer is None or lines_layer is None:
            return None
        model = RplLayerSync(points_layer, lines_layer, rpl_id).load_model()
        self._model_cache[rpl_id] = model
        return model

    def invalidate_cache(self, rpl_id: Optional[str] = None):
        if rpl_id is None:
            self._model_cache.clear()
        else:
            self._model_cache.pop(rpl_id, None)

    # --------------------------------------------------------- bathymetry --
    def bathymetry_profile(self, rpl_id: str, kp_km: float, back_km: float,
                           fwd_km: float, step_m: float = 25.0
                           ) -> List[Tuple[float, float]]:
        """(distance_from_kp_m, depth_m) pairs along the route corridor.

        Depth comes from the RPL's configured depth sources where available,
        falling back to the RPL's own ApproxDepth interpolation. Distances
        are metres relative to ``kp_km`` (negative = behind), matching the
        simulator's along-route x axis.
        """
        model = self._model(rpl_id)
        if model is None:
            return []
        kp_lo = max(model.start_kp_km(), kp_km - back_km)
        kp_hi = min(model.end_kp_km(), kp_km + fwd_km)
        if kp_hi <= kp_lo:
            return []

        service = None
        row = self.store.get_rpl(rpl_id)
        if row:
            config = DepthSourceConfig(self.store.rpl_depth_config(rpl_id))
            if config.is_configured():
                service = DepthService(config, self.project)
                if not service.is_available():
                    service = None

        out: List[Tuple[float, float]] = []
        step_km = max(step_m, 1.0) / 1000.0
        kp = kp_lo
        while kp <= kp_hi + 1e-9:
            k = min(kp, kp_hi)
            depth = None
            if service is not None:
                pos = rpl_engine.point_at_kp(model, k, self.da)
                if pos is not None:
                    depth = service.sample(pos[0], pos[1])
            if depth is None:
                depth = _interp_rpl_depth(model, k)
            if depth is not None:
                out.append(((k - kp_km) * 1000.0, float(depth)))
            kp += step_km
        return out

    def lay_azimuth_deg(self, rpl_id: str, kp_km: float) -> Optional[float]:
        model = self._model(rpl_id)
        if model is None:
            return None
        return rpl_engine.bearing_at_kp(model, kp_km)

    # ----------------------------------------------------------- assembly --
    def assembly_window(self, rpl_id: str, kp_km: float, cable_back_m: float,
                        cable_fwd_m: float) -> List[Dict]:
        """Catenary-JSON-compatible slice of the fitted assembly around a KP.

        Requires a stored wb_fit linking an assembly to this RPL. Boundary
        sections are length-trimmed so the window's total cable length is
        exactly ``cable_back_m + cable_fwd_m`` (clamped to the assembly).
        """
        fits = self.store.list_fits(rpl_id=rpl_id)
        if not fits:
            return []
        fit_row = fits[0]
        header, item_rows = self.store.get_assembly(fit_row.get("assembly_id") or "")
        if header is None:
            return []
        assembly = am.assembly_from_rows(header, item_rows)
        model = self._model(rpl_id)
        if model is None:
            return []

        anchor_kp = float(fit_row.get("anchor_kp_km") or 0.0)
        anchor_cable_m = float(fit_row.get("anchor_cable_dist_m") or 0.0)
        direction = 1 if int(fit_row.get("direction") or 1) >= 0 else -1

        anchor_cable_km = rpl_engine.cable_dist_from_kp(model, anchor_kp)
        cable_here_km = rpl_engine.cable_dist_from_kp(model, kp_km)
        if anchor_cable_km is None or cable_here_km is None:
            return []
        centre_m = anchor_cable_m + direction * (cable_here_km - anchor_cable_km) * 1000.0

        window_lo = max(0.0, centre_m - cable_back_m)
        window_hi = min(assembly.total_length_m(), centre_m + cable_fwd_m)
        if window_hi <= window_lo:
            return []

        starts = assembly.cable_dist_starts_m()
        window_assembly = am.Assembly(name=f"{assembly.name} (window)", kind=assembly.kind)
        for i, item in enumerate(assembly.items):
            start = starts[i]
            if item.is_section:
                end = start + (item.length_m or 0.0)
                overlap_lo = max(start, window_lo)
                overlap_hi = min(end, window_hi)
                if overlap_hi <= overlap_lo:
                    continue
                trimmed = am.AssemblyItem(**{**item.__dict__, "item_id": schema.new_id()})
                trimmed.length_m = overlap_hi - overlap_lo
                window_assembly.items.append(trimmed)
            else:
                if window_lo <= start <= window_hi:
                    window_assembly.items.append(item)

        import json

        return json.loads(am.to_catenary_json(window_assembly))

    # -------------------------------------------------------------- output --
    def push_results(self, rpl_id: str, label: str, kp_series: List[Dict]) -> Optional[str]:
        """Write simulator results back as a KP-referenced point layer.

        ``kp_series``: [{"kp_km": float, ...numeric/string fields...}, ...].
        Positions are resolved on the route; returns the created layer name.
        """
        model = self._model(rpl_id)
        if model is None or not kp_series:
            return None
        from ..processing.cable_lay_parsers import WKT_KEY

        extra_keys: List[str] = []
        for row in kp_series:
            for key in row:
                if key not in ("kp_km",) and key not in extra_keys:
                    extra_keys.append(key)

        specs = [("kp_km", "float")]
        for key in extra_keys:
            sample = next((r[key] for r in kp_series if r.get(key) is not None), None)
            specs.append((key, "float" if isinstance(sample, (int, float)) else "str"))

        rows = []
        for entry in kp_series:
            kp = entry.get("kp_km")
            if kp is None:
                continue
            pos = rpl_engine.point_at_kp(model, float(kp), self.da)
            if pos is None:
                continue
            row = {"kp_km": float(kp)}
            for key in extra_keys:
                row[key] = entry.get(key)
            row[WKT_KEY] = f"POINT ({pos[1]} {pos[0]})"
            rows.append(row)
        if not rows:
            return None

        layer_name = f"wb_v3_{schema.sanitize_slug(label)}"
        self.store.write_spatial_layer(layer_name, specs, QgsWkbTypes.Point, rows)
        return layer_name


def _interp_rpl_depth(model: rpl_engine.RplModel, kp_km: float) -> Optional[float]:
    """Linear interpolation of the RPL's own ApproxDepth at a KP."""
    pts = [p for p in model.points if p.dist_cum_km is not None and p.depth_m is not None]
    if len(pts) < 2:
        return pts[0].depth_m if pts else None
    if kp_km <= pts[0].dist_cum_km:
        return pts[0].depth_m
    if kp_km >= pts[-1].dist_cum_km:
        return pts[-1].depth_m
    for i in range(len(pts) - 1):
        k0, k1 = pts[i].dist_cum_km, pts[i + 1].dist_cum_km
        if k0 <= kp_km <= k1:
            if k1 - k0 <= 0:
                return pts[i].depth_m
            t = (kp_km - k0) / (k1 - k0)
            return pts[i].depth_m + t * (pts[i + 1].depth_m - pts[i].depth_m)
    return None
