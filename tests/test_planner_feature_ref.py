# -*- coding: utf-8 -*-
"""QGIS geometry checks for planner feature references and route frames."""

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas

from ..planner.feature_ref import (
    FeatureReferenceResolver, feature_reference, shared_reference,
)


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _line_layer(crs, wkt, name):
    layer = QgsVectorLayer("LineString?crs=%s" % crs, name, "memory")
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt(wkt))
    layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer, next(layer.getFeatures())


def test_projected_and_source_fallback():
    project = QgsProject.instance()
    project.removeAllMapLayers()
    layer, feature = _line_layer("EPSG:3857", "LINESTRING(0 0, 1000 0)", "Projected")
    canvas = QgsMapCanvas()
    canvas.setDestinationCrs(layer.crs())
    resolver = FeatureReferenceResolver(project, canvas)
    ref = feature_reference(layer, feature, "Route A")
    ref["layer_id"] = "missing-layer-id"  # force persisted-source fallback
    frame = resolver.route_frame(ref)
    midpoint = resolver.point_at_chainage(ref, 500.0)
    ok = frame is not None and abs(frame.total_length_m - 1000.0) < 2.0
    ok = ok and midpoint is not None and abs(midpoint.x() - 500.0) < 2.0
    return _result("projected length + layer source fallback", ok,
                   "length=%s" % (frame.total_length_m if frame else None))


def test_geographic_length():
    project = QgsProject.instance()
    project.removeAllMapLayers()
    layer, feature = _line_layer("EPSG:4326", "LINESTRING(0 0, 0.01 0)", "Geographic")
    canvas = QgsMapCanvas()
    canvas.setDestinationCrs(layer.crs())
    resolver = FeatureReferenceResolver(project, canvas)
    frame = resolver.route_frame(feature_reference(layer, feature, "Equator"))
    ok = frame is not None and 1100.0 < frame.total_length_m < 1125.0
    return _result("geographic ellipsoidal length", ok,
                   "length=%s" % (frame.total_length_m if frame else None))


def test_long_geographic_transit():
    project = QgsProject.instance()
    project.removeAllMapLayers()
    layer, feature = _line_layer("EPSG:4326", "LINESTRING(0 0, 60 0)", "Long transit")
    canvas = QgsMapCanvas()
    canvas.setDestinationCrs(layer.crs())
    resolver = FeatureReferenceResolver(project, canvas)
    frame = resolver.route_frame(feature_reference(layer, feature, "Equatorial transit"))
    # WGS84 equatorial arc: 60 degrees * 111,319.490793 m/degree.
    expected_m = 6679169.4476
    ok = frame is not None and abs(frame.total_length_m - expected_m) < 5.0
    return _result("long geographic transit remains ellipsoidal", ok,
                   "length=%s" % (frame.total_length_m if frame else None))


def test_playback_follows_planned_line_without_losing_geodesic_length():
    project = QgsProject.instance()
    project.removeAllMapLayers()
    layer, feature = _line_layer(
        "EPSG:4326", "LINESTRING(-30 60, 30 60)", "High-latitude transit")

    geographic_canvas = QgsMapCanvas()
    geographic_canvas.setDestinationCrs(layer.crs())
    geographic = FeatureReferenceResolver(project, geographic_canvas).route_frame(
        feature_reference(layer, feature, "Planned route"))
    if geographic is None:
        return _result("playback follows planned line with geodesic length", False, "no frame")
    midpoint = geographic.point_at_kp(geographic.total_length_km / 2.0)
    first_half = geographic.extract_segment(0.0, geographic.total_length_km / 2.0)
    first_half_points = first_half.asPolyline() if first_half else []
    ok = 3_200_000.0 < geographic.total_length_m < 3_300_000.0
    ok = ok and midpoint is not None and abs(midpoint.x()) < 1e-8
    ok = ok and abs(midpoint.y() - 60.0) < 1e-8
    ok = ok and first_half_points and abs(first_half_points[-1].y() - 60.0) < 1e-8

    projected_canvas = QgsMapCanvas()
    projected_canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    projected = FeatureReferenceResolver(project, projected_canvas).route_frame(
        feature_reference(layer, feature, "Planned route"))
    projected_mid = projected.point_at_kp(projected.total_length_km / 2.0) if projected else None
    projected_geom = projected.geometries[0] if projected else None
    on_rendered_route = (
        projected_geom.distance(QgsGeometry.fromPointXY(projected_mid))
        if projected_geom is not None and projected_mid is not None else float("inf"))
    ok = ok and projected is not None
    ok = ok and abs(projected.total_length_m - geographic.total_length_m) < 0.5
    ok = ok and on_rendered_route < 1e-5
    return _result(
        "playback follows planned line with geodesic length", ok,
        "length=%.3fkm midpoint=(%.6f, %.6f) projected_offset=%g" % (
            geographic.total_length_km, midpoint.x(), midpoint.y(), on_rendered_route))


def test_shared_reference_resolves_through_owner():
    """A task sharing another task's owned geometry follows the live point."""
    import os
    import tempfile

    from ..planner import schema
    from ..planner.store import PlannerStore

    project = QgsProject.instance()
    project.removeAllMapLayers()
    folder = tempfile.mkdtemp(prefix="pow_shared_ref_test_")
    store = PlannerStore(os.path.join(folder, "planner.gpkg"))
    store.ensure_created()
    scenario_id = store.create_scenario("Shared refs", "2026-01-01T00:00")
    owner_id = schema.new_id()
    reference = store.set_task_geometry(
        owner_id, scenario_id, 0, "Owner", QgsGeometry.fromWkt("POINT(3 4)"),
        "point", source_crs=QgsCoordinateReferenceSystem("EPSG:4326"),
        source_kind="test")
    canvas = QgsMapCanvas()
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    resolver = FeatureReferenceResolver(project, canvas, store)
    sharer = {"task_id": schema.new_id()}
    sharer.update(shared_reference(reference, owner_id))
    resolved = resolver.resolve(sharer)
    ok = resolved is not None and resolved.geom_kind == "point"
    if resolved is not None:
        point = resolved.feature.geometry().asPoint()
        ok = ok and abs(point.x() - 3.0) < 1e-9 and abs(point.y() - 4.0) < 1e-9
    location = resolver.location_point(sharer)
    ok = ok and location is not None and abs(location.x() - 3.0) < 1e-6
    # Owner geometry gone -> sharer degrades to unresolved, not a stale id.
    store.delete_task_geometries([owner_id])
    resolver.clear_cache()
    ok = ok and resolver.resolve(sharer) is None
    return _result("shared reference resolves through owning task", ok)


def run_all():
    return [
        test_projected_and_source_fallback(),
        test_geographic_length(),
        test_long_geographic_transit(),
        test_playback_follows_planned_line_without_losing_geodesic_length(),
        test_shared_reference_resolves_through_owner(),
    ]
