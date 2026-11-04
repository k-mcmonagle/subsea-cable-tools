"""Clip CPT projection to uninterrupted mapped soil units (no interpolation).

The map is a lateral constraint on an existing KP/depth section, not evidence
of the unit continuing down through the CPT. Unknown or conflicting mapping
removes coverage. Influence never jumps a gap to another patch of the same unit.
"""
from bisect import bisect_left
from collections import defaultdict

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsGeometry, QgsPointXY, QgsProject, QgsSpatialIndex)

from .numeric_profile_geometry import polygon_assignments


def unit_runs(assignments, breaks=()):
    """Disjoint (start, end, unit) runs; overlapping different units are gaps.

    Equal-class polygons may meet/overlap. NULL/blank units remain unknown,
    including where they overlap a classified polygon. Route-part breaks
    prevent two disconnected spatial segments merging just because KP joins.
    """
    events = defaultdict(list)
    for row in assignments:
        a, b = row["start_kp"], row["end_kp"]
        if b > a:
            events[a].append((row["source_id"], 1))
            events[b].append((row["source_id"], -1))
    breaks = set(breaks)
    for kp in breaks:
        events[kp]  # ensure the sweep splits here even inside a polygon
    active, out = {}, []
    marks = sorted(events)
    for i, a in enumerate(marks[:-1]):
        for unit, delta in events[a]:
            active[unit] = active.get(unit, 0) + delta
            if not active[unit]:
                del active[unit]
        b = marks[i + 1]
        if len(active) != 1:
            continue
        unit = next(iter(active))
        if not unit:
            continue
        if out and out[-1][1] == a and out[-1][2] == unit and a not in breaks:
            out[-1] = (out[-1][0], b, unit)
        else:
            out.append((a, b, unit))
    return out


def constrain_cpts(route, layer, unit_field, assignments, cpts_by_name, kps_by_name):
    """Clip existing midpoint/edited ranges; return (ranges, human notes).

    Require an unambiguous unit at the actual CPT and an uninterrupted
    matching unit at its projected KP. The straight map connection from the
    CPT to the route must also be covered by that unit. This avoids transferring
    a test from a separate same-class pocket across a rock strip or mapping gap.
    """
    from .numeric_datasets import attribute_text
    if layer is None:
        raise ValueError("The saved soil-unit layer is unavailable. Restore it or choose another in Edit → KP ranges.")
    if layer.geometryType() != 2:
        raise ValueError("The soil-unit source must be a polygon layer.")
    if unit_field not in layer.fields().names():
        raise ValueError(f"Soil layer '{layer.name()}' has no unit field '{unit_field}'. Choose it in Edit → KP ranges.")
    if route is None:
        raise ValueError("Select a route before constraining CPTs to soil units.")
    if not layer.crs().isValid():
        raise ValueError(f"Soil layer '{layer.name()}' has no valid CRS.")
    mapped, _ = polygon_assignments(route, layer, unit_field, include_empty=True)
    breaks, previous = [], None
    for p, q, a, _b in route.measured_segments():
        if previous is not None and previous != p:
            breaks.append(a)
        previous = q
    runs = unit_runs(mapped, breaks)
    starts = [r[0] for r in runs]
    index, polygons = QgsSpatialIndex(), {}
    for feature in layer.getFeatures():
        polygons[feature.id()] = (QgsGeometry(feature.geometry()), attribute_text(feature[unit_field]))
        index.addFeature(feature)
    project = QgsProject.instance()
    cpt_transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), layer.crs(), project)
    route_transform = QgsCoordinateTransform(route._distance.sourceCrs(), layer.crs(), project)
    out, skipped, clipped = [], [], 0
    for assignment in assignments:
        name = assignment["source_id"]
        cpt, kp = cpts_by_name.get(name), kps_by_name.get(name)
        if cpt is None or kp is None:
            skipped.append(f"{name}: no CPT position")
            continue
        point = cpt_transform.transform(QgsPointXY(float(cpt["lon"]), float(cpt["lat"])))
        point_geometry = QgsGeometry.fromPointXY(point)
        units = {unit for fid in index.intersects(point_geometry.boundingBox())
                 for geometry, unit in [polygons[fid]] if geometry.intersects(point_geometry)}
        if len(units) != 1 or not next(iter(units), ""):
            skipped.append(f"{name}: missing or ambiguous soil unit at CPT")
            continue
        unit = next(iter(units))
        pos = bisect_left(starts, kp)
        candidates = [r for r in runs[max(0, pos - 1):pos + 1] if r[0] <= kp <= r[1] and r[2] == unit]
        if len(candidates) != 1:
            skipped.append(f"{name}: projected KP has no unambiguous continuous '{unit}' range")
            continue
        route_point = route.point_at_kp(kp, clamp=True)
        if route_point is None:
            skipped.append(f"{name}: projected KP is outside the route")
            continue
        snapped = route_transform.transform(route_point)
        if point != snapped:
            connector = QgsGeometry.fromPolylineXY([point, snapped])
            same, other = [], []
            for fid in index.intersects(connector.boundingBox()):
                geometry, value = polygons[fid]
                (same if value == unit else other).append(geometry)
            coverage = QgsGeometry.unaryUnion(same) if same else QgsGeometry()
            missing = connector.difference(coverage)
            conflict = connector.intersection(QgsGeometry.unaryUnion(other)) if other else QgsGeometry()
            if missing.lastError() or conflict.lastError() or coverage.lastError():
                raise ValueError("Could not intersect the soil mapping with a CPT projection. Check polygon validity.")
            tolerance = max(connector.length() * 1e-8, 1e-10)
            if missing.length() > tolerance or conflict.length() > tolerance:
                skipped.append(f"{name}: projection crosses another unit or a mapping gap")
                continue
        a, b, _ = candidates[0]
        lo, hi = max(assignment["start_kp"], a), min(assignment["end_kp"], b)
        if hi <= lo:
            skipped.append(f"{name}: no range remains after soil limits")
            continue
        clipped += int(lo > assignment["start_kp"] or hi < assignment["end_kp"])
        out.append(dict(assignment, start_kp=lo, end_kp=hi,
                        flags="; ".join(filter(None, [assignment.get("flags"), "soil constrained"])),
                        source_ref=f"CPT positions; mapped soil unit {unit} ({layer.name()})"))
    notes = [f"Soil constraints: {len(out)} of {len(assignments)} CPT ranges retained; "
             f"{clipped} shortened, {len(skipped)} omitted. Mapping gaps and conflicting units remain unknown."]
    notes.extend(skipped[:8])
    if len(skipped) > 8:
        notes.append(f"{len(skipped) - 8} further CPTs omitted")
    return out, notes
