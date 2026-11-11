"""Ellipsoidal annotation measurements, independent of route extent or map CRS.

QgsDistanceArea uses GeographicLib for inverse geodesics. ECEF chord distances
are lower bounds used only to find candidates, never as the measured distance.
Polygon predicates use a separate ellipsoidal AEQD centred on each target; its
radial distances are metres on WGS84. Source edges are adaptively subdivided
before projection, rather than replacing a curved transformed edge by a chord.
"""

import heapq
import math

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
    QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingException, QgsRectangle,
    QgsSpatialIndex,
)

from ..qgis_compat import GEOMETRY_POLYGON


CURVE_ERROR = 0.002  # metres; comparisons include a conservative numerical margin
BOUNDARY_MARGIN = 2 * CURVE_ERROR


def geographic(point):
    x, y = point.x(), point.y()
    if not math.isfinite(x) or not math.isfinite(y) or abs(y) > 90 or abs(x) > 1e12:
        raise ValueError('Coordinate cannot be represented on the WGS84 ellipsoid.')
    return QgsPointXY((x + 180) % 360 - 180, y)


def point_of(line):
    return QgsPointXY(line.x, line.y)


class EllipsoidalMetric:
    def __init__(self, context):
        self.crs = QgsCoordinateReferenceSystem('EPSG:4326')
        self.area = QgsDistanceArea()
        self.area.setSourceCrs(self.crs, context.transformContext())
        if not self.area.setEllipsoid('WGS84') or not self.area.willUseEllipsoid():
            raise QgsProcessingException('Could not initialize WGS84 ellipsoidal measurements.')
        a, b = self.area.ellipsoidSemiMajor(), self.area.ellipsoidSemiMinor()
        self.a, self.e2 = a, 1 - (b / a) ** 2

    def metres(self, a, b):
        result = self.area.measureLine(a, b)
        if not math.isfinite(result) or result < 0:
            raise ValueError('Ellipsoidal distance could not be measured.')
        return result

    def distance(self, a, b):
        return self.metres(point_of(a), point_of(b))

    def angle(self, a, b):
        if self.metres(a, b) <= 1e-8:
            raise ValueError('Text orientation is undefined at this coordinate; inspect the rotation field.')
        return 90 - math.degrees(self.area.bearing(a, b))

    def offset(self, a, b):
        distance = self.distance(a, b)
        if distance <= 1e-8:
            return 0.0, 0.0
        delta = math.radians(90 - a.angle) - self.area.bearing(point_of(a), point_of(b))
        return distance * math.cos(delta), distance * math.sin(delta)

    def compatible(self, a, b, tolerance):
        delta = a.angle - b.angle
        if self.distance(a, b) > 1e-6:
            # Compare orientations after transporting along the joining geodesic.
            # Meridians can converge rapidly near a pole.
            forward = self.area.bearing(point_of(a), point_of(b))
            reverse = self.area.bearing(point_of(b), point_of(a))
            delta += math.degrees(forward - reverse) + 180
        return abs((delta + 180) % 360 - 180) <= tolerance

    def xyz(self, point):
        lon, lat = math.radians(point.x()), math.radians(point.y())
        n = self.a / math.sqrt(1 - self.e2 * math.sin(lat) ** 2)
        return (n * math.cos(lat) * math.cos(lon), n * math.cos(lat) * math.sin(lon),
                n * (1 - self.e2) * math.sin(lat))

    def position(self, line):
        return tuple(round(v, 6) for v in self.xyz(point_of(line)))


class GlobalPointIndex:
    """Balanced 3D tree, including neighbours across the date line and poles."""

    def __init__(self, positions, metric, cancel):
        self.positions, self.metric, self.cancel = positions, metric, cancel
        self.xyz = {key: metric.xyz(point) for key, point in positions.items()}

        def build(keys, depth=0):
            if not keys:
                return None
            cancel()
            axis = depth % 3
            keys.sort(key=lambda key: self.xyz[key][axis])
            mid = len(keys) // 2
            return keys[mid], axis, build(keys[:mid], depth + 1), build(keys[mid + 1:], depth + 1)

        self.root = build(list(positions))

    def _search(self, point, radius=None, count=None):
        xyz, found, heap = self.metric.xyz(point), [], []
        radius2 = (radius + 1e-6) ** 2 if radius is not None else math.inf
        visited = 0

        def visit(node):
            nonlocal visited
            if node is None:
                return
            visited += 1
            if visited % 1000 == 0:
                self.cancel()
            key, axis, left, right = node
            delta = xyz[axis] - self.xyz[key][axis]
            near, far = (left, right) if delta < 0 else (right, left)
            distance2 = sum((a - b) ** 2 for a, b in zip(xyz, self.xyz[key]))
            if count is None:
                if distance2 <= radius2:
                    found.append(key)
            elif len(heap) < count:
                heapq.heappush(heap, (-distance2, key))
            elif distance2 < -heap[0][0]:
                heapq.heapreplace(heap, (-distance2, key))
            visit(near)
            bound = radius2 if count is None else (-heap[0][0] if len(heap) == count else math.inf)
            if delta * delta <= bound:
                visit(far)

        visit(self.root)
        return found if count is None else [key for _, key in heap]

    def near(self, point, radius):
        result = {}
        for key in self._search(point, radius=radius):
            distance = self.metric.metres(point, self.positions[key])
            if distance <= radius:
                result[key] = distance
        return result

    def nearest(self, point, count):
        seeds = self._search(point, count=count)
        if not seeds:
            return []
        # Chord ordering is not necessarily geodesic ordering. Search every
        # candidate within the largest seed's geodesic distance before ranking.
        radius = max(self.metric.metres(point, self.positions[key]) for key in seeds)
        close = self.near(point, radius + 1e-6)
        return sorted(close, key=lambda key: (close[key], key))[:count]


def local_crs(point):
    crs = QgsCoordinateReferenceSystem()
    crs.createFromProj(f'+proj=aeqd +lat_0={point.y():.15g} +lon_0={point.x():.15g} +datum=WGS84 +units=m +no_defs')
    if not crs.isValid():
        raise ValueError('Could not create local ellipsoidal measurement coordinates.')
    return crs


def polygon_parts(geometry):
    if geometry.isNull() or geometry.isEmpty():
        return []
    if geometry.type() == GEOMETRY_POLYGON and not geometry.isMultipart():
        return [geometry]
    result = []
    for part in geometry.asGeometryCollection():
        if part.type() == GEOMETRY_POLYGON or part.isMultipart():
            result.extend(polygon_parts(part))
    return result


class LocalPolygon:
    def __init__(self, geometry, source_crs, context, cancel):
        self.rings = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
        self.source_crs, self.context, self.cancel = source_crs, context, cancel
        self.cap_centre, self.cap_radius = None, None
        self.to_global = QgsCoordinateTransform(source_crs, QgsCoordinateReferenceSystem('EPSG:4326'), context.transformContext())

    def at(self, target):
        transform = QgsCoordinateTransform(self.source_crs, local_crs(target), self.context.transformContext())
        cache, count = {}, 0

        def project(point):
            nonlocal count
            key = point.x(), point.y()
            if key not in cache:
                count += 1
                if count % 1000 == 0:
                    self.cancel()
                if count > 200000:
                    raise ValueError('Polygon boundary is too complex to measure reliably; review this geometry.')
                value = transform.transform(point)
                if not all(math.isfinite(v) for v in (value.x(), value.y())) or math.hypot(value.x(), value.y()) > 19500000:
                    raise ValueError('Polygon spans the opposite side of the globe from this target; review its geometry.')
                cache[key] = value
            return cache[key]

        def segment(a, b, pa, pb, depth=0):
            # Preserve straight edges in their source CRS. Geographic edges use
            # the short longitude interval when they cross +/-180 degrees.
            mid = QgsPointXY((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
            pm = project(mid)
            checks = [(pm, 0.5)]
            for fraction in (0.25, 0.75):
                checks.append((project(QgsPointXY(a.x() + fraction * (b.x() - a.x()),
                                                 a.y() + fraction * (b.y() - a.y()))), fraction))
            error = max(math.hypot(p.x() - (pa.x() + f * (pb.x() - pa.x())),
                                   p.y() - (pa.y() + f * (pb.y() - pa.y()))) for p, f in checks)
            if error <= CURVE_ERROR and pa.distance(pb) <= 10000:
                return [pb]
            if depth >= 30:
                raise ValueError('Polygon edge cannot be transformed to the measurement tolerance; review its geometry.')
            return segment(a, mid, pa, pm, depth + 1) + segment(mid, b, pm, pb, depth + 1)

        polygons = []
        for polygon in self.rings:
            rings = []
            for ring in polygon:
                self.cancel()
                if len(ring) < 4:
                    raise ValueError('Polygon ring has too few vertices.')
                points = [project(ring[0])]
                for a, b in zip(ring, ring[1:]):
                    if self.source_crs.isGeographic():
                        b = QgsPointXY(a.x() + ((b.x() - a.x() + 180) % 360 - 180), b.y())
                    points.extend(segment(a, b, project(a), project(b)))
                points[-1] = points[0]
                rings.append(points)
            polygons.append(QgsGeometry.fromPolygonXY(rings))
        geometry = QgsGeometry.collectGeometry(polygons)
        repaired = not geometry.isGeosValid()
        if repaired:
            geometry = QgsGeometry.collectGeometry(polygon_parts(geometry.makeValid()))
        if geometry.isNull() or geometry.isEmpty() or geometry.type() != GEOMETRY_POLYGON or not geometry.isGeosValid():
            raise ValueError('Polygon cannot be used for reliable spatial matching.')
        return geometry, repaired

def cap_rectangles(target, radius):
    """Conservative lon/lat boxes for an ellipsoidal distance cap.

    WGS84's minimum curvature radius is greater than 6,330,000 m, so a cap
    on that smaller sphere contains every point within the ellipsoidal radius.
    """
    angle = min(math.pi, (radius + BOUNDARY_MARGIN) / 6330000)
    lat = math.radians(target.y())
    south, north = max(-90, target.y() - math.degrees(angle)), min(90, target.y() + math.degrees(angle))
    if abs(lat) + angle >= math.pi / 2:
        return [QgsRectangle(-180, south, 180, north)]
    width = math.degrees(math.asin(min(1, math.sin(angle) / math.cos(lat))))
    left, right = target.x() - width, target.x() + width
    rectangles = [QgsRectangle(max(-180, left), south, min(180, right), north)]
    if left < -180:
        rectangles.append(QgsRectangle(left + 360, south, 180, north))
    if right > 180:
        rectangles.append(QgsRectangle(-180, south, right - 360, north))
    return rectangles


class GlobalPolygonIndex:
    def __init__(self, metric):
        self.index = QgsSpatialIndex()
        self.metric, self.polygons, self.ids = metric, {}, {}

    def add(self, key, polygon):
        self.polygons[key] = polygon
        rectangles = (cap_rectangles(polygon.cap_centre, polygon.cap_radius) if polygon.cap_centre is not None
                      else [QgsRectangle(-180, -90, 180, 90)])
        for rectangle in rectangles:
            index_id = len(self.ids)
            self.ids[index_id] = key
            feature = QgsFeature(index_id)
            feature.setGeometry(QgsGeometry.fromRect(rectangle))
            self.index.addFeature(feature)

    def candidates(self, target, radius):
        keys = {self.ids[key] for rectangle in cap_rectangles(target, radius) for key in self.index.intersects(rectangle)}
        return sorted(key for key in keys if self.polygons[key].cap_centre is None
                      or self.metric.metres(target, self.polygons[key].cap_centre)
                      <= self.polygons[key].cap_radius + radius + BOUNDARY_MARGIN)
