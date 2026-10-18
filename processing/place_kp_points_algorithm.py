import math

from qgis.core import (
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessing,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsProcessingException,
    QgsFeatureSink
)
from .algorithm_base import SubseaCableAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING, PROCESSING_NUMBER_DOUBLE
from qgis.PyQt.QtCore import QCoreApplication
from ..kp_range_utils import (
    make_distance_area,
    add_distance_mode_parameter,
    read_distance_mode,
)
from ..kp_geo_utils import get_features_skip_invalid, ordered_route_geometry
from .kp_common import START_KP_HELP, add_start_kp_parameter, build_route, read_start_kp, report_kp_basis

class PlaceKpPointsAlgorithm(SubseaCableAlgorithm):
    """
    This algorithm places points along a line layer at specified regular intervals.
    """
    INPUT_LINE = 'INPUT_LINE'
    INPUT_RASTER = 'INPUT_RASTER'
    OUTPUT = 'OUTPUT'
    INTERVAL_1KM = 'INTERVAL_1KM'
    INTERVAL_50KM = 'INTERVAL_50KM'
    INTERVAL_100KM = 'INTERVAL_100KM'
    INTERVAL_CUSTOM = 'INTERVAL_CUSTOM'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINE,
                self.tr('Reference Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                self.tr('Optional MBES Raster for Depth'),
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.INTERVAL_1KM,
                self.tr('1 km Interval'),
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.INTERVAL_50KM,
                self.tr('50 km Interval'),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.INTERVAL_100KM,
                self.tr('100 km Interval'),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.INTERVAL_CUSTOM,
                self.tr('Custom Interval (Kilometers)'),
                type=PROCESSING_NUMBER_DOUBLE,
                defaultValue=None,
                optional=True,
                minValue=0.001
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output KP(Z) Point Layer')
            )
        )

        add_distance_mode_parameter(self)
        add_start_kp_parameter(self)

    def processAlgorithm(self, parameters, context, feedback):
        line_layer = self.parameterAsSource(parameters, self.INPUT_LINE, context)
        if line_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_LINE))

        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)

        line_to_raster_xform = None
        if raster_layer and line_layer.sourceCrs() != raster_layer.crs():
            feedback.pushInfo(self.tr(
                "Line layer and raster layer use different CRSes; sample points will be reprojected to the raster CRS."
            ))
            line_to_raster_xform = QgsCoordinateTransform(
                line_layer.sourceCrs(), raster_layer.crs(), context.transformContext()
            )

        intervals_to_process = []
        if self.parameterAsBoolean(parameters, self.INTERVAL_1KM, context):
            intervals_to_process.append(1.0)
        if self.parameterAsBoolean(parameters, self.INTERVAL_50KM, context):
            intervals_to_process.append(50.0)
        if self.parameterAsBoolean(parameters, self.INTERVAL_100KM, context):
            intervals_to_process.append(100.0)
        
        custom_interval = self.parameterAsDouble(parameters, self.INTERVAL_CUSTOM, context)
        if custom_interval > 0:
            intervals_to_process.append(custom_interval)

        if not intervals_to_process:
            feedback.pushInfo(self.tr("No intervals selected."))
            return {self.OUTPUT: None}

        raster_sampler = None
        null_depth_count = 0
        if raster_layer:
            if not raster_layer.isValid():
                raise QgsProcessingException(self.tr("Invalid raster layer provided."))
            provider = raster_layer.dataProvider()
            if not provider.isValid():
                raise QgsProcessingException(self.tr("Invalid raster data provider."))
            raster_sampler = provider

        fields = QgsFields()
        fields.append(QgsField("source_line", FIELD_TYPE_STRING))
        fields.append(QgsField("label", FIELD_TYPE_STRING))
        fields.append(QgsField("kp", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("reverse_kp", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("interval_km", FIELD_TYPE_DOUBLE))
        if raster_sampler:
            fields.append(QgsField("depth", FIELD_TYPE_DOUBLE))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, QgsWkbTypes.Point, line_layer.sourceCrs()
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        source_line_name = line_layer.sourceName()

        line_features = list(get_features_skip_invalid(line_layer))
        if not line_features:
            feedback.pushInfo(self.tr("Input line layer has no features."))
            return {self.OUTPUT: None}

        geometries = [f.geometry() for f in line_features if f.hasGeometry() and not f.geometry().isEmpty()]
        skipped = len(line_features) - len(geometries)
        if skipped:
            feedback.pushWarning(self.tr(
                f"{skipped} feature(s) with no usable geometry were skipped."))
        if not geometries:
            feedback.pushInfo(self.tr("Input line layer has no usable geometries."))
            return {self.OUTPUT: None}
        # Shared route builder (SeqNo/layer order, no noding).
        merged_geometry = ordered_route_geometry(line_features)
        
        if merged_geometry.isEmpty():
            feedback.pushInfo(self.tr("Geometry is empty after merging features."))
            return {self.OUTPUT: None}

        distance_mode = read_distance_mode(self, parameters, context)
        try:
            distance_calculator = make_distance_area(
                line_layer.sourceCrs(), context.transformContext(),
                mode=distance_mode, project=context.project(),
            )
        except ValueError as exc:
            raise QgsProcessingException(str(exc))

        start_kp = read_start_kp(self, parameters, context)
        route = build_route([merged_geometry], distance_calculator, start_kp)
        report_kp_basis(feedback, route, start_kp)
        total_length = route.total_length_m
        if total_length == 0:
            feedback.pushInfo(self.tr("Line has no length."))
            return {self.OUTPUT: None}
        end_kp = route.end_kp_km

        nulls = [0]

        def depth_at(point):
            if not raster_sampler:
                return []
            sample_point = point
            if line_to_raster_xform is not None:
                try:
                    sample_point = line_to_raster_xform.transform(point)
                except Exception:
                    nulls[0] += 1
                    return [None]
            depth_value, res = raster_sampler.sample(sample_point, 1)
            if not res:
                nulls[0] += 1
            return [float(depth_value)] if res else [None]

        def place(kp_val, label, interval_km):
            point = route.point_at_kp(kp_val, clamp=True)
            if point is None:
                return 0
            feat = QgsFeature(fields)
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
            attributes = [source_line_name, label, kp_val, round(end_kp - kp_val, 3), interval_km] + depth_at(point)
            feat.setAttributes(attributes)
            sink.addFeature(feat, QgsFeatureSink.FastInsert)
            return 1

        def kp_label(kp_val):
            return f"KP {kp_val:.0f}" if float(kp_val).is_integer() else f"KP {round(kp_val, 6):g}"

        # A point at the route start (the start KP), then at round KPs: multiples
        # of each interval counted from KP 0, along the route's KP path.
        points_placed = place(start_kp, kp_label(start_kp), 0.0)
        for interval_km in intervals_to_process:
            k = math.floor(start_kp / interval_km + 1e-9) + 1
            while True:
                if feedback.isCanceled():
                    return {self.OUTPUT: None}
                kp_val = round(k * interval_km, 9)
                if kp_val >= end_kp - 1e-9:
                    break
                points_placed += place(kp_val, kp_label(kp_val), interval_km)
                k += 1
        null_depth_count = nulls[0]
        if raster_sampler and null_depth_count > 0:
            feedback.pushWarning(self.tr(f"{null_depth_count} point(s) were outside the raster's extent and have been assigned null depth values."))

        feedback.pushInfo(self.tr(f"Placed {points_placed} points."))
        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
This tool places points at regular intervals along a line layer. If an optional raster layer is provided, a 'depth' attribute will be added to the points by sampling the raster.

**Instructions:**

1.  **Input Line Layer:** Select the line layer you want to process. The tool will treat all features in the layer as a single continuous line.
2.  **Optional MBES Raster for Depth:** Optionally, select a raster layer (e.g., a bathymetry model) to sample depth values from. **Important:** The line and raster layers must be in the same Coordinate Reference System (CRS).
3.  **Select Intervals:**
    *   Check the boxes for standard intervals (1 km, 50 km, 100 km).
    *   You can select multiple standard intervals.
    *   Optionally, provide a **Custom Interval** in kilometers.
4.  **Run:** Execute the tool. A new point layer will be created with a point at the start KP and at each multiple of each interval (round KPs). The points will have attributes for KP, reverse KP (end KP - KP), the interval distance, and depth (if a raster was provided).
""") + "\n\n" + self.tr(START_KP_HELP)

    def name(self):
        return 'placekppointsalongroute'

    def displayName(self):
        return self.tr('Place KP(Z) Points Along Route')

    def group(self):
        return self.tr('KP Points')

    def groupId(self):
        return 'kppoints'

    def createInstance(self):
        return PlaceKpPointsAlgorithm()

    def tr(self, string):
        return QCoreApplication.translate("PlaceKpPointsAlgorithm", string)
