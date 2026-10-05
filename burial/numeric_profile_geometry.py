"""Polygon assignment against the selected RouteFrame, preserving every crossing."""
from qgis.core import (QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsPointXY,
                       QgsProject, QgsSpatialIndex)

from ..kp_geo_utils import iter_line_parts


def polygon_assignments(route, layer, id_field):
    if route is None:
        raise ValueError("Select a route before assigning profiles.")
    if id_field not in layer.fields().names():
        raise ValueError("Select the polygon investigation ID field.")
    # Segment-local fractions avoid snapping a crossing to a different limb
    # of a looping or retraced route. Lengths already use the route KP mode.
    segments = list(route.measured_segments())
    index = QgsSpatialIndex()
    for i, (p, q, _a, _b) in enumerate(segments):
        feature = QgsFeature(i)
        feature.setGeometry(QgsGeometry.fromPolylineXY([p, q]))
        index.addFeature(feature)
    transform = QgsCoordinateTransform(layer.crs(), route._distance.sourceCrs(), QgsProject.instance())
    assignments, warnings = [], []
    for feature in layer.getFeatures():
        value = feature[id_field]
        source = "" if value is None or str(value) == "NULL" else str(value).strip()
        if not source:
            warnings.append(f"Polygon {feature.id()}: missing investigation ID")
            continue
        polygon = QgsGeometry(feature.geometry())
        if polygon.isEmpty() or not polygon.isGeosValid():
            raise ValueError(f"Polygon {feature.id()} ({source}) has empty or invalid geometry.")
        polygon.transform(transform)
        spans = []
        for i in sorted(index.intersects(polygon.boundingBox())):
            p, q, start, end = segments[i]
            segment = QgsGeometry.fromPolylineXY([p, q])
            intersection = segment.intersection(polygon)
            if intersection.lastError():
                raise ValueError(f"Polygon {feature.id()}: {intersection.lastError()}")
            dx, dy = q.x() - p.x(), q.y() - p.y()
            norm = dx * dx + dy * dy
            # Mixed line/point intersections occur at polygon tangencies.
            parts = (intersection.asGeometryCollection() if intersection.isMultipart() else [intersection])
            lines = [line for geometry in parts for line in iter_line_parts(geometry)]
            for part in lines:
                fractions = [max(0.0, min(1.0, ((v.x() - p.x()) * dx + (v.y() - p.y()) * dy) / norm))
                             for v in part] if norm else []
                if not fractions:
                    continue
                lo, hi = min(fractions), max(fractions)
                if hi <= lo:
                    continue
                a, b = start + lo * (end - start), start + hi * (end - start)
                first = QgsPointXY(p.x() + lo * dx, p.y() + lo * dy)
                last = QgsPointXY(p.x() + hi * dx, p.y() + hi * dy)
                spans.append((a, b, first, last))
        merged = []
        for a, b, first, last in sorted(spans, key=lambda s: s[0]):
            if merged and abs(merged[-1][1] - a) < 1e-10 and merged[-1][3] == first:
                merged[-1] = (merged[-1][0], b, merged[-1][2], last)
            else:
                merged.append((a, b, first, last))
        if not merged:
            warnings.append(f"{source}: polygon {feature.id()} does not cross the route")
        for a, b, _first, _last in merged:
            assignments.append({"source_id": source, "start_kp": a, "end_kp": b,
                                "src_start_kp": a, "src_end_kp": b, "flags": "",
                                "source_ref": layer.name(), "layer_id": layer.id(),
                                "feature_id": feature.id()})
    return assignments, warnings
