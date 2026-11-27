"""Build draft BAS sections on the plan's measured route."""
from collections import defaultdict

from qgis.core import NULL, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsProject

from . import bas_model, schema
from .numeric_profile_geometry import RoutePolygonIntersections


def generate_sections(route, sources, columns, scope, rpl_label="", progress=None):
    """Sources are (polygon layer, selected field names). Empty fields use the layer name."""
    overlay = RoutePolygonIntersections(route)
    route_crs = route._distance.sourceCrs()
    start, end = route.start_kp_km, route.end_kp_km
    if scope is not None and scope.end_km > scope.start_km:
        start, end = max(start, scope.start_km), min(end, scope.end_km)
    if end <= start:
        raise ValueError("The plan scope does not intersect its current route.")
    section = route.extract_segment(start, end)
    if section is None or section.isEmpty():
        raise ValueError("The plan scope has no measurable route geometry.")
    bounds = section.boundingBox()
    columns = [dict(col) for col in columns]
    field_keys = {}

    def column_for(field):
        if field not in field_keys:
            label = field or "Soil layer"
            existing = next((col for col in columns if col["label"] == label
                             and col["kind"] == bas_model.KIND_TEXT), None)
            if existing is None:
                labels = {col["label"] for col in columns}
                base, number = label, 2
                while label in labels:
                    label = f"{base} ({number})"
                    number += 1
                existing = bas_model.make_column(label, taken=[col["key"] for col in columns])
                columns.append(existing)
            field_keys[field] = existing["key"]
        return field_keys[field]

    # Each event adds/removes a polygon or the part of the route on its boundary.
    events = defaultdict(list)
    start, end = round(start, 10), round(end, 10)
    events[start], events[end] = [], []
    values = {}
    matches = 0

    def add_spans(spans, identity, boundary):
        for a, b in spans:
            a, b = max(start, round(a, 10)), min(end, round(b, 10))
            if b > a:
                events[a].append((identity, boundary, True))
                events[b].append((identity, boundary, False))

    for layer, fields in sources:
        if not layer.crs().isValid() or not route_crs.isValid():
            raise ValueError(f"Check the coordinate reference system for '{layer.name()}' and the route.")
        if any(field not in layer.fields().names() for field in fields):
            raise ValueError(f"A selected field is missing from '{layer.name()}'.")
        transform = QgsCoordinateTransform(layer.crs(), route_crs, QgsProject.instance())
        to_layer = QgsCoordinateTransform(route_crs, layer.crs(), QgsProject.instance())
        request = QgsFeatureRequest().setFilterRect(to_layer.transformBoundingBox(bounds))
        request.setSubsetOfAttributes(list(fields), layer.fields())
        keys = {field: column_for(field) for field in fields or [None]}
        for i, feature in enumerate(layer.getFeatures(request)):
            if progress and i % 32 == 0 and not progress(layer.name()):
                return [], columns
            polygon = QgsGeometry(feature.geometry())
            if polygon.isEmpty() or not polygon.isGeosValid():
                raise ValueError(f"Repair geometry {feature.id()} in '{layer.name()}' before generating sections.")
            polygon.transform(transform)
            identity = (layer.id(), feature.id())
            spans = overlay.spans(polygon)
            if not any(min(end, b) > max(start, a) for a, b in spans):
                continue
            matches += 1
            attributes = {}
            for field, key in keys.items():
                raw = feature[field] if field is not None else layer.name()
                attributes[key] = "" if raw is None or raw == NULL else str(raw).strip()
            values[identity] = attributes
            add_spans(spans, identity, False)
            edge = QgsGeometry(polygon.constGet().boundary())
            add_spans(overlay.spans(edge), identity, True)

    if not matches:
        return [], columns
    rows, active, boundaries = [], set(), set()
    stations = sorted(events)
    for a, b in zip(stations, stations[1:]):
        for identity, boundary, entering in sorted(events[a], key=lambda event: event[2]):
            current = boundaries if boundary else active
            if entering:
                current.add(identity)
            else:
                current.discard(identity)
        notes = []
        if not active:
            notes.append("Gap in polygon coverage")
        if len(active) > 1:
            notes.append(f"Overlap of {len(active)} polygons")
        if boundaries:
            notes.append("Route follows a polygon boundary")
        combined = defaultdict(set)
        missing = False
        for identity in active:
            for key, value in values[identity].items():
                if value:
                    combined[key].add(value)
                else:
                    missing = True
        if missing:
            notes.append("Missing classification value")
        rows.append(bas_model.decode_row({
            "row_id": schema.new_id(), "start_kp": a, "end_kp": b, "src_start_kp": a,
            "src_end_kp": b, "src_rpl": rpl_label,
            "values": {key: " / ".join(sorted(items)) for key, items in combined.items()},
            "notes": "Review — " + ". ".join(notes) if notes else ""}))
    return rows, columns
