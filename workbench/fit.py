# -*- coding: utf-8 -*-
"""Assembly -> route fitting.

Answers "where does joint 3 land on the seabed?": walks an assembly in the
cable domain from an anchor, converts each body's cumulative cable distance
to route KP through the RPL's per-segment slack, and interpolates the
position (and optionally depth) at that KP.

Depends only on rpl_engine (pure) plus an optional position resolver and
depth sampler injected by the caller, so it runs headless. UI callers pass a
RouteFrame-backed ``point_at_kp_fn`` for geodesic interpolation and a
DepthService-backed ``depth_fn``; tests use the engine's own interpolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .assembly_model import Assembly, AssemblyItem
from . import rpl_engine
from .rpl_engine import RplModel

# Positions within this distance of the route ends snap onto the route
# instead of being reported "off route" (RPL documents round to metres).
BOUNDS_TOLERANCE_M = 1.0


@dataclass
class FitAnchor:
    """Where cable distance ``cable_dist_m`` of the assembly sits on the route."""
    kp_km: float
    cable_dist_m: float = 0.0
    direction: int = 1  # +1: assembly runs with increasing KP; -1: against


@dataclass
class BodyLanding:
    item: AssemblyItem
    cable_dist_m: float           # position within the assembly
    kp_km: Optional[float]        # route KP (None if off the route)
    lat: Optional[float] = None
    lon: Optional[float] = None
    depth_m: Optional[float] = None
    on_route: bool = True


@dataclass
class SectionSpan:
    item: AssemblyItem
    cable_start_m: float
    cable_end_m: float
    kp_start_km: Optional[float]
    kp_end_km: Optional[float]
    clipped: bool = False  # True when part of the section falls off the route


@dataclass
class FitResult:
    bodies: List[BodyLanding] = field(default_factory=list)
    sections: List[SectionSpan] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def build_fit_mapping(store, fit_row: Dict):
    """Efficient cable->KP closure for a stored wb_fit row.

    Loads the RPL model once and returns ``mapping(cable_m) -> Optional[kp_km]``
    (None when the position is off the route), suitable for the SLD's KP axis
    which calls it on every repaint. Returns None if the RPL can't be loaded.
    """
    from .rpl_layer_io import RplLayerSync

    rpl = store.get_rpl(fit_row.get("rpl_id") or "")
    if not rpl:
        return None
    points_layer = store.open_layer(rpl.get("points_layer"))
    lines_layer = store.open_layer(rpl.get("lines_layer"))
    if points_layer is None or lines_layer is None:
        return None
    model = RplLayerSync(points_layer, lines_layer).load_model()

    anchor_kp = float(fit_row.get("anchor_kp_km") or 0.0)
    anchor_cable_m = float(fit_row.get("anchor_cable_dist_m") or 0.0)
    direction = 1 if int(fit_row.get("direction") or 1) >= 0 else -1
    anchor_cable_km = rpl_engine.cable_dist_from_kp(model, anchor_kp)
    if anchor_cable_km is None:
        return None

    def mapping(cable_m: float) -> Optional[float]:
        route_cable_km = anchor_cable_km + direction * (cable_m - anchor_cable_m) / 1000.0
        return rpl_engine.kp_from_cable_dist(model, route_cable_km)

    return mapping


def _route_cable_bounds_km(model: RplModel) -> Tuple[float, float]:
    start = model.points[0].cable_dist_cum_km or 0.0
    end = model.points[-1].cable_dist_cum_km or 0.0
    return start, end


def fit_assembly(
    assembly: Assembly,
    model: RplModel,
    anchor: FitAnchor,
    *,
    point_at_kp_fn: Optional[Callable[[float], Optional[Tuple[float, float]]]] = None,
    depth_fn: Optional[Callable[[float, float], Optional[float]]] = None,
    da=None,
) -> FitResult:
    """Fit ``assembly`` onto the RPL ``model`` from ``anchor``.

    The anchor maps assembly cable distance ``anchor.cable_dist_m`` to route
    KP ``anchor.kp_km``; every other assembly position follows through the
    route's cable-distance profile (i.e. through the per-segment slack).

    ``point_at_kp_fn(kp_km) -> (lat, lon)`` resolves positions; when omitted
    and ``da`` (QgsDistanceArea) is given, the engine's linear interpolation
    is used. ``depth_fn(lat, lon) -> depth_m`` optionally samples depth.
    """
    result = FitResult()
    if not model.points or len(model.points) < 2:
        result.warnings.append("RPL has too few positions to fit against.")
        return result

    anchor_cable_km = rpl_engine.cable_dist_from_kp(model, anchor.kp_km)
    if anchor_cable_km is None:
        result.warnings.append(
            f"Anchor KP {anchor.kp_km:.3f} is outside the route (KP "
            f"{model.start_kp_km():.3f}–{model.end_kp_km():.3f})."
        )
        return result

    direction = 1 if anchor.direction >= 0 else -1
    route_cable_min_km, route_cable_max_km = _route_cable_bounds_km(model)
    # Snap positions within this distance of the route ends onto the route.
    # An assembly extracted from the same RPL sums per-segment cable distances,
    # which differs from the cumulative end by float dust (and real documents
    # round to metres) — a strict bounds check would push the last body "off
    # route" spuriously.
    tolerance_km = BOUNDS_TOLERANCE_M / 1000.0

    def route_cable_km_of(assembly_cable_m: float) -> float:
        """Assembly cable position (m) -> route cable-distance coordinate (km)."""
        offset_km = (assembly_cable_m - anchor.cable_dist_m) / 1000.0
        return anchor_cable_km + direction * offset_km

    def kp_of(assembly_cable_m: float) -> Optional[float]:
        route_cable_km = route_cable_km_of(assembly_cable_m)
        if route_cable_km < route_cable_min_km - tolerance_km \
                or route_cable_km > route_cable_max_km + tolerance_km:
            return None
        route_cable_km = min(max(route_cable_km, route_cable_min_km), route_cable_max_km)
        return rpl_engine.kp_from_cable_dist(model, route_cable_km)

    def overrun_m(assembly_cable_m: float) -> float:
        """How far (m) beyond the route this cable position falls (0 if on)."""
        route_cable_km = route_cable_km_of(assembly_cable_m)
        if route_cable_km < route_cable_min_km:
            return (route_cable_min_km - route_cable_km) * 1000.0
        if route_cable_km > route_cable_max_km:
            return (route_cable_km - route_cable_max_km) * 1000.0
        return 0.0

    def resolve(kp_km: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
        if kp_km is None:
            return None, None
        if point_at_kp_fn is not None:
            pos = point_at_kp_fn(kp_km)
        elif da is not None:
            pos = rpl_engine.point_at_kp(model, kp_km, da)
        else:
            pos = None
        return (pos[0], pos[1]) if pos else (None, None)

    starts = assembly.cable_dist_starts_m()
    total_m = assembly.total_length_m()

    # over/under-run checks
    start_kp = kp_of(0.0)
    end_kp = kp_of(total_m)
    if start_kp is None or end_kp is None:
        run_start = overrun_m(0.0)
        run_end = overrun_m(total_m)
        detail = []
        if run_start > 0:
            detail.append(f"{run_start:.1f} m before the route start")
        if run_end > 0:
            detail.append(f"{run_end:.1f} m past the route end")
        result.warnings.append(
            "Assembly extends beyond the route ("
            + " and ".join(detail) + "). Adjust the anchor KP/cable distance, "
            "or accept that the overhanging items have no landing position."
        )

    for i, item in enumerate(assembly.items):
        cable_start = starts[i]
        if item.is_section:
            cable_end = cable_start + (item.length_m or 0.0)
            kp_start = kp_of(cable_start)
            kp_end = kp_of(cable_end)
            result.sections.append(SectionSpan(
                item=item,
                cable_start_m=cable_start,
                cable_end_m=cable_end,
                kp_start_km=kp_start,
                kp_end_km=kp_end,
                clipped=(kp_start is None) or (kp_end is None),
            ))
        else:
            kp = kp_of(cable_start)
            lat, lon = resolve(kp)
            depth = depth_fn(lat, lon) if (depth_fn and lat is not None) else None
            result.bodies.append(BodyLanding(
                item=item,
                cable_dist_m=cable_start,
                kp_km=kp,
                lat=lat,
                lon=lon,
                depth_m=depth,
                on_route=kp is not None,
            ))

    off_route = [b for b in result.bodies if not b.on_route]
    if off_route:
        details = ", ".join(
            f"{b.item.name or 'body'} ({overrun_m(b.cable_dist_m):.1f} m over)"
            for b in off_route[:5]
        )
        result.warnings.append(f"{len(off_route)} bodies land off the route: {details}")
    return result
