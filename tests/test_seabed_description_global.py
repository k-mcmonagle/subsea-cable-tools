"""Synthetic world-spanning routes, date-line/polar geometry and metric tolerances."""

import math
import random

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProcessingContext

from ..processing.annotation_geodesy import EllipsoidalMetric, GlobalPointIndex, LocalPolygon, local_crs
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING
from .test_seabed_description_association import layer, rows, run, absent
from .test_seabed_description_usability import reproject


def scene(centres, boundary_offset=None, anchor_gap=0):
    context = QgsProcessingContext()
    metric = EllipsoidalMetric(context)
    text, leaders, polygons = [], [], []
    for i, (lon, lat) in enumerate(centres):
        lower = QgsPointXY(lon, lat)
        upper = metric.area.computeSpheroidProject(lower, 10, 0)
        text.extend([(QgsGeometry.fromPointXY(upper).asWkt(17), [f'Soil {i}']),
                     (QgsGeometry.fromPointXY(lower).asWkt(17), ['second line'])])
        target = metric.area.computeSpheroidProject(lower, 300, math.radians(135))
        transform = QgsCoordinateTransform(local_crs(target), metric.crs, context.transformContext())
        ring = [transform.transform(QgsPointXY(x, y)) for x, y in
                [(-20, -20), (20, -20), (20, 20), (-20, 20), (-20, -20)]]
        polygons.append((QgsGeometry.fromPolygonXY([ring]).asWkt(17), []))
        if boundary_offset is not None:
            target = transform.transform(QgsPointXY(20 - boundary_offset, 0))
        anchor = metric.area.computeSpheroidProject(upper, anchor_gap, math.pi / 2)
        leaders.append((QgsGeometry.fromPolylineXY([anchor, target]).asWkt(17), []))
    return (layer('Point', 'Global descriptions', text, [('label_text', FIELD_TYPE_STRING)], 'EPSG:4326'),
            layer('MultiLineString', 'Global leaders', leaders, crs='EPSG:4326'),
            layer('MultiPolygon', 'Global soil', polygons, crs='EPSG:4326'))


def test_world_spanning_route_auto_spacing_and_original_crs():
    text, leaders, polygons = scene([(-160, 10), (40, 70), (160, -65), (179.9999, 45), (-10, 89.9), (90, -89.9)])
    outputs, context, project, _ = run(text, [leaders], [polygons], TEXT_FIELD='', MAX_LINE_GAP=0)
    assert outputs['OUTPUT_LABELS'].featureCount() == 6
    assert {f['assembled_text'] for f in rows(outputs['OUTPUT_LABELS'])} == {f'Soil {i}\nsecond line' for i in range(6)}
    assert {f['description_status'] for f in rows(outputs['OUTPUT_POLYGONS'])} == {'matched'}
    assert {f['match_status'] for f in rows(outputs['OUTPUT_CHECKS'])} == {'matched'}
    assert all(output.crs().authid() == 'EPSG:4326' for output in outputs.values())
    assert text.featureCount() == 12 and polygons.featureCount() == 6


def test_metre_tolerances_at_poles_dateline_and_in_web_mercator():
    for centre in [(179.9999, 60), (0, 89.9), (130, -89.9), (-30, 80)]:
        for boundary_offset, expected in [(0.2, 'review'), (0.8, 'matched'), (-0.2, 'review'), (-2, 'unmatched')]:
            text, leaders, polygons = scene([centre], boundary_offset=boundary_offset)
            if centre[1] == 80:
                text, leaders, polygons = [reproject(source, 'EPSG:3857') for source in (text, leaders, polygons)]
            outputs, context, project, _ = run(text, [leaders], [polygons])
            result = rows(outputs['OUTPUT_POLYGONS'])[0]
            assert result['description_status'] == expected, (centre, boundary_offset, result.attributes())
            if expected != 'matched':
                assert absent(result['soil_description'])
        for anchor_gap, expected in [(0.8, 'matched'), (1.2, 'unmatched')]:
            text, leaders, polygons = scene([centre], anchor_gap=anchor_gap)
            outputs, context, project, _ = run(text, [leaders], [polygons])
            assert rows(outputs['OUTPUT_CHECKS'])[0]['match_status'] == expected


def test_global_neighbour_index_matches_brute_force_geodesics():
    metric = EllipsoidalMetric(QgsProcessingContext())
    rng = random.Random(7321)
    positions = {i: QgsPointXY(rng.uniform(-180, 180), rng.uniform(-90, 90)) for i in range(150)}
    for i, point in enumerate([QgsPointXY(179.999999, 0), QgsPointXY(-179.999999, 0),
                               QgsPointXY(0, 89.99999), QgsPointXY(180, 89.99999),
                               QgsPointXY(90, -89.99999), QgsPointXY(-90, -89.99999)], 150):
        positions[i] = point
    index = GlobalPointIndex(positions, metric, lambda: None)
    for point in list(positions.values())[::9] + list(positions.values())[-6:]:
        distances = {key: metric.metres(point, other) for key, other in positions.items()}
        assert index.nearest(point, 13) == sorted(distances, key=lambda key: (distances[key], key))[:13]
        for radius in (0.5, 3, 1000000, 20010000):
            assert set(index.near(point, radius)) == {key for key, value in distances.items() if value <= radius}
    assert abs(metric.metres(QgsPointXY(0, 0), QgsPointXY(1, 0)) - 111319.49079327357) < 1e-6


def test_text_block_crosses_date_line_without_a_partition_seam():
    # A north/south annotation rotated by 90 degrees reads west to east here.
    text = layer('Point', 'Date-line text', [
        ('Point(179.99996 0)', ['first', 90]), ('Point(-179.99996 0)', ['second', 90]),
    ], [('label_text', FIELD_TYPE_STRING), ('rotation', FIELD_TYPE_DOUBLE)], 'EPSG:4326')
    outputs, context, project, _ = run(text, ROTATION_FIELD='rotation')
    assert outputs['OUTPUT_LABELS'].featureCount() == 1
    assert rows(outputs['OUTPUT_LABELS'])[0]['assembled_text'] == 'first\nsecond'


def test_geographic_polar_cap_and_hole_are_measured_locally():
    # A geographic ring encircling the pole is not a valid planar lat/lon area.
    context = QgsProcessingContext()
    metric = EllipsoidalMetric(context)
    crs = QgsCoordinateReferenceSystem('EPSG:4326')
    polygon = QgsGeometry.fromPolygonXY([[QgsPointXY(lon, 89.8) for lon in (-135, -45, 45, 135, -135)]])
    local = LocalPolygon(polygon, crs, context, lambda: None)
    geometry, repaired = local.at(QgsPointXY(0, 90))
    assert not repaired and geometry.contains(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))
    distance = QgsGeometry(geometry.constGet().boundary()).distance(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))
    assert abs(distance - metric.metres(QgsPointXY(0, 90), QgsPointXY(0, 89.8))) < 0.01
    hole = [QgsPointXY(lon, 89.9) for lon in (-135, -45, 45, 135, -135)]
    polygon = QgsGeometry.fromPolygonXY([polygon.asPolygon()[0], hole])
    geometry, repaired = LocalPolygon(polygon, crs, context, lambda: None).at(QgsPointXY(0, 90))
    assert not repaired and not geometry.contains(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))


def test_long_sparse_polygon_keeps_its_curved_transformed_boundary():
    # This boundary spans sixteen degrees but has only its original endpoints.
    # Transforming those endpoints alone would move the edge far from the target.
    metric = EllipsoidalMetric(QgsProcessingContext())
    bottom = QgsPointXY(0, 50.02)
    top = metric.area.computeSpheroidProject(bottom, 10, 0)
    text = layer('Point', 'Long route annotations', [
        (QgsGeometry.fromPointXY(top).asWkt(17), ['Fine sediment']),
        (QgsGeometry.fromPointXY(bottom).asWkt(17), ['second line']),
    ], [('label_text', FIELD_TYPE_STRING)], 'EPSG:4326')
    polygons = layer('MultiPolygon', 'Long soil polygon', [
        ('Polygon((-8 50,8 50,8 50.1,-8 50.1,-8 50))', []),
    ], crs='EPSG:4326')
    for distance, expected in [(0.2, 'review'), (0.8, 'matched')]:
        target = metric.area.computeSpheroidProject(QgsPointXY(0, 50), distance, 0)
        leaders = layer('MultiLineString', 'Long route leader', [
            (QgsGeometry.fromPolylineXY([top, target]).asWkt(17), []),
        ], crs='EPSG:4326')
        outputs, context, project, _ = run(text, [leaders], [polygons])
        assert rows(outputs['OUTPUT_POLYGONS'])[0]['description_status'] == expected
        measured = rows(outputs['OUTPUT_CHECKS'])[0]['boundary_gap_m']
        assert abs(measured - distance) < 0.005, (distance, measured)


def test_bad_geographic_outlier_does_not_reject_valid_route():
    text, leaders, polygons = scene([(-10, 45), (80, -60)])
    records = [(feature.geometry().asWkt(17), feature.attributes()) for feature in text.getFeatures()]
    records.append(('Point(0 95)', ['Bad coordinate']))
    text = layer('Point', 'Includes invalid latitude', records, [('label_text', FIELD_TYPE_STRING)], 'EPSG:4326')
    outputs, context, project, _ = run(text, [leaders], [polygons])
    assert outputs['OUTPUT_LABELS'].featureCount() == 3
    assert sum(f['fragment_count'] for f in rows(outputs['OUTPUT_LABELS'])) == 5
    assert sum(f['text_status'] == 'review' for f in rows(outputs['OUTPUT_LABELS'])) == 1
    assert {f['description_status'] for f in rows(outputs['OUTPUT_POLYGONS'])} == {'matched'}
