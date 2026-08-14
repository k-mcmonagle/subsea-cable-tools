# -*- coding: utf-8 -*-
"""PlaceOutlineAlongRouteAlgorithm

Place a body-fixed outline (ship, plough, trencher — e.g. the output of
Import Ship Outline (DXF)) on a route at one or more KPs, oriented to the
local route heading. Placement is metre-true: each KP is placed in its
local UTM zone and the heading is measured between projected route points,
so grid convergence is handled implicitly.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsWkbTypes,
)

from ..burial import footprint
from ..burial import geometry2d
from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..qgis_compat import (
    FIELD_TYPE_DOUBLE,
    FIELD_TYPE_STRING,
    PROCESSING_NUMBER_DOUBLE,
)

_WGS84 = "EPSG:4326"


class PlaceOutlineAlongRouteAlgorithm(QgsProcessingAlgorithm):
    OUTLINE = "OUTLINE"
    ROUTE = "ROUTE"
    KPS = "KPS"
    KP_START = "KP_START"
    KP_END = "KP_END"
    INTERVAL_M = "INTERVAL_M"
    HEADING_OFFSET = "HEADING_OFFSET"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate(
            "PlaceOutlineAlongRouteAlgorithm", string)

    def createInstance(self):
        return PlaceOutlineAlongRouteAlgorithm()

    def name(self):
        return "place_outline_along_route"

    def displayName(self):
        return self.tr("Place Outline Along Route (KP)")

    def group(self):
        return self.tr("Other Tools")

    def groupId(self):
        return "other_tools"

    def shortHelpString(self):
        return self.tr("""
Place a scaled outline (ship, plough, trencher…) on a route at chosen KPs, \
rotated to the local route heading — scale context for charts and plans.

**Outline:** a body-fixed outline centred on its reference point at the \
origin with the bow/front along +Y, in metres — exactly what "Import Ship \
Outline (DXF)" produces.

**KPs:** either an explicit comma-separated list, or a series from Start \
KP to End KP every Interval metres (leave the list empty to use the \
series; set Interval to 0 for a single placement at Start KP).

KP chainage is measured ellipsoidally on the project ellipsoid \
(WGS84 fallback), matching the plugin's other KP tools. Each placement is \
done in the local UTM zone, so the outline stays metre-true and the \
heading (written to the output) accounts for grid convergence. Output is \
in EPSG:4326 with kp / heading_deg / source attributes.
""")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.OUTLINE, self.tr("Outline layer (body-fixed, from Import Ship Outline)"),
            types=[QgsProcessing.TypeVectorPolygon, QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ROUTE, self.tr("Route (line)"),
            types=[QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterString(
            self.KPS, self.tr("KP list (comma separated, e.g. 1.5, 12.3)"),
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.KP_START, self.tr("Series start KP (km)"),
            type=PROCESSING_NUMBER_DOUBLE, defaultValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.KP_END, self.tr("Series end KP (km)"),
            type=PROCESSING_NUMBER_DOUBLE, defaultValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.INTERVAL_M, self.tr("Series interval (m, 0 = single placement)"),
            type=PROCESSING_NUMBER_DOUBLE, defaultValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.HEADING_OFFSET, self.tr("Heading offset (degrees)"),
            type=PROCESSING_NUMBER_DOUBLE, defaultValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, self.tr("Placed outlines")))

    def processAlgorithm(self, parameters, context, feedback):
        outline_layer = self.parameterAsVectorLayer(
            parameters, self.OUTLINE, context)
        route_source = self.parameterAsSource(parameters, self.ROUTE, context)
        if outline_layer is None or route_source is None:
            raise QgsProcessingException("Outline and route are required.")
        kps_text = self.parameterAsString(parameters, self.KPS, context)
        kp_start = self.parameterAsDouble(parameters, self.KP_START, context)
        kp_end = self.parameterAsDouble(parameters, self.KP_END, context)
        interval_m = self.parameterAsDouble(
            parameters, self.INTERVAL_M, context)
        heading_offset = self.parameterAsDouble(
            parameters, self.HEADING_OFFSET, context)

        kps = geometry2d.parse_kp_list(kps_text)
        if not kps:
            kps = geometry2d.kp_series(kp_start, kp_end, interval_m)
        if not kps:
            raise QgsProcessingException("No KPs to place.")

        # Body-fixed outline: collect every feature into one geometry.
        geoms = []
        source_name = ""
        for feat in outline_layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            geoms.append(QgsGeometry(geom))
            if not source_name:
                try:
                    source_name = str(feat["source_file"] or "")
                except (KeyError, TypeError):
                    source_name = ""
        if not geoms:
            raise QgsProcessingException(
                "The outline layer has no usable geometry.")
        outline = (QgsGeometry.collectGeometry(geoms)
                   if len(geoms) > 1 else geoms[0])
        source_name = source_name or outline_layer.name()

        # WGS84 route frame + ellipsoidal chainage: the Burial Planner /
        # KP-tools convention, so KPs here match the rest of the plugin.
        wgs84 = QgsCoordinateReferenceSystem(_WGS84)
        distance = make_distance_area(
            wgs84, context.transformContext(),
            project=context.project() or QgsProject.instance())
        try:
            route = RouteFrame.from_source(
                route_source, distance, target_crs=wgs84,
                project=context.project() or QgsProject.instance(),
                follow_stored_geometry=True)
        except Exception as exc:
            raise QgsProcessingException(
                f"The route could not be prepared: {exc}")

        fields = QgsFields()
        fields.append(QgsField("kp", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("heading_deg", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("source", FIELD_TYPE_STRING))
        wkb = QgsWkbTypes.multiType(outline_layer.wkbType())
        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, wkb, wgs84)
        if sink is None:
            raise QgsProcessingException("The output sink could not be created.")

        total = max(len(kps), 1)
        placed = 0
        for i, kp in enumerate(kps):
            if feedback.isCanceled():
                break
            geom, heading = footprint.place_outline(
                outline, route, kp, heading_offset_deg=heading_offset,
                target_crs=wgs84,
                transform_context=context.transformContext())
            if geom is None or geom.isEmpty():
                feedback.pushInfo(f"KP {kp:.3f}: could not be placed — skipped.")
                continue
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            feat.setAttribute("kp", round(float(kp), 6))
            feat.setAttribute("heading_deg", round(float(heading), 2))
            feat.setAttribute("source", source_name)
            sink.addFeature(feat)
            placed += 1
            feedback.setProgress(100.0 * (i + 1) / total)
        feedback.pushInfo(f"Placed {placed} outline(s) at {len(kps)} KP(s).")
        return {self.OUTPUT: dest_id}
