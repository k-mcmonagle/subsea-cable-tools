# -*- coding: utf-8 -*-
"""QGIS-side helpers for KP re-referencing: open a Workbench RPL revision
as a RouteFrame and sample the source→target correspondence.

Everything numeric happens in ``kp_rereference`` (pure); this module only
turns route geometry into ``(source_kp, target_kp, offset_m)`` triples.
Both routes are WGS84 ``RouteFrame``s built the same way the plan builds
its own (``analysis_task.build_route_frame``): ellipsoidal chainage,
features ordered by SeqNo, stored-geometry interpolation.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from . import kp_rereference
from .analysis_task import build_route_frame


def rpl_label(rpl: dict) -> str:
    name = str(rpl.get("name") or "RPL")
    rev = str(rpl.get("rev_label") or "").strip()
    if rev and rev.casefold() in name.casefold():
        return name
    return f"{name} — {rev}" if rev else name


def open_route_for_rpl(workbench_store, rpl_id: str, project):
    """``(RouteFrame, label)`` for a Workbench RPL; raises ValueError."""
    if workbench_store is None:
        raise ValueError("The Cable Workbench store is not available.")
    rpl = workbench_store.get_rpl(rpl_id)
    if rpl is None:
        raise ValueError("The selected RPL is no longer in the Workbench.")
    layer = workbench_store.open_layer(rpl.get("lines_layer") or "")
    if layer is None or not layer.isValid():
        raise ValueError(f"The lines layer of '{rpl_label(rpl)}' could not "
                         "be opened.")
    route, _distance = build_route_frame(layer, project)
    if route is None:
        raise ValueError(f"'{rpl_label(rpl)}' has no usable route geometry.")
    return route, rpl_label(rpl)


def sample_correspondence(src_route, dst_route, step_km: float = 0.05,
                          start_kp: Optional[float] = None,
                          end_kp: Optional[float] = None
                          ) -> List[Tuple[float, float, float]]:
    """Walk the source route and project every station onto the target."""
    total = float(src_route.total_length_km)
    lo = 0.0 if start_kp is None else max(0.0, float(start_kp))
    hi = total if end_kp is None else min(total, float(end_kp))
    if hi <= lo:
        return []
    step = max(0.001, float(step_km))
    count = int(math.floor((hi - lo) / step))
    stations = [lo + i * step for i in range(count + 1)]
    if stations[-1] < hi - 1e-9:
        stations.append(hi)
    out: List[Tuple[float, float, float]] = []
    for kp in stations:
        point = src_route.point_at_kp(kp, clamp=True)
        if point is None:
            continue
        hit = dst_route.kp_at_point(point)
        if hit is None or hit.feature_index < 0:
            continue
        dcc = float(hit.dcc_m)
        out.append((kp, float(hit.kp_km), dcc if math.isfinite(dcc) else float("inf")))
    return out


def geometry_map(src_route, dst_route, step_km: float = 0.05,
                 offset_tol_m: float = 25.0, source_label: str = "",
                 target_label: str = "") -> kp_rereference.KpMap:
    samples = sample_correspondence(src_route, dst_route, step_km=step_km)
    return kp_rereference.build_from_samples(
        samples, offset_tol_m=offset_tol_m,
        source_label=source_label, target_label=target_label)
