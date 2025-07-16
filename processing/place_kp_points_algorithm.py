from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessing,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsProcessingException,
    QgsFeatureSink,
    QgsDistanceArea
)
from qgis.PyQt.QtCore import QVariant, QCoreApplication

class PlaceKpPointsAlgorithm(QgsProcessingAlgorithm):
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
                self.tr('Input Line Layer'),
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
                type=QgsProcessingParameterNumber.Double,
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

    def processAlgorithm(self, parameters, context, feedback):
        line_layer = self.parameterAsSource(parameters, self.INPUT_LINE, context)
        if line_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_LINE))

        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)

        if raster_layer and line_layer.sourceCrs() != raster_layer.crs():
            raise QgsProcessingException(self.tr("Input line layer and raster layer must have the same CRS."))

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
        fields.append(QgsField("source_line", QVariant.String))
        fields.append(QgsField("label", QVariant.String))
        fields.append(QgsField("kp", QVariant.Double))
        fields.append(QgsField("reverse_kp", QVariant.Double))
        fields.append(QgsField("interval_km", QVariant.Double))
        if raster_sampler:
            fields.append(QgsField("depth", QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, QgsWkbTypes.Point, line_layer.sourceCrs()
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        source_line_name = line_layer.sourceName()

        line_features = list(line_layer.getFeatures())
        if not line_features:
            feedback.pushInfo(self.tr("Input line layer has no features."))
            return {self.OUTPUT: None}

        geometries = [f.geometry() for f in line_features]
        merged_geometry = QgsGeometry.unaryUnion(geometries)
        
        if merged_geometry.isEmpty():
            feedback.pushInfo(self.tr("Geometry is empty after merging features."))
            return {self.OUTPUT: None}

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(line_layer.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        total_length = distance_calculator.measureLength(merged_geometry)
        total_length_km = round(total_length / 1000, 3)
        if total_length == 0:
            feedback.pushInfo(self.tr("Line has no length."))
            return {self.OUTPUT: None}

        line_parts = merged_geometry.asMultiPolyline() if merged_geometry.isMultipart() else [merged_geometry.asPolyline()]
        
        # Place point at the start of the line (KP 0)
        start_point = line_parts[0][0]
        feat = QgsFeature(fields)
        feat.setGeometry(QgsGeometry.fromPointXY(start_point))
        label = 'KP 0'
        attributes = [source_line_name, label, 0.0, total_length_km, 0.0]
        if raster_sampler:
            depth_value, res = raster_sampler.sample(start_point, 1)
            if res:
                attributes.append(float(depth_value))
            else:
                attributes.append(None)
                null_depth_count += 1
        feat.setAttributes(attributes)
        sink.addFeature(feat, QgsFeatureSink.FastInsert)
        points_placed = 1

        for interval_km in intervals_to_process:
            interval_m = interval_km * 1000
            cumulative_length = 0.0
            next_kp_dist = interval_m

            for part in line_parts:
                for i in range(len(part) - 1):
                    if feedback.isCanceled():
                        return {self.OUTPUT: None}

                    p1, p2 = part[i], part[i+1]
                    segment_length = distance_calculator.measureLine(p1, p2)
                    
                    while next_kp_dist < cumulative_length + segment_length:
                        dist_into_segment = next_kp_dist - cumulative_length
                        ratio = dist_into_segment / segment_length if segment_length > 0 else 0
                        
                        x = p1.x() + ratio * (p2.x() - p1.x())
                        y = p1.y() + ratio * (p2.y() - p1.y())
                        point_geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                        
                        feat = QgsFeature(fields)
                        feat.setGeometry(point_geom)
                        kp_val = next_kp_dist / 1000
                        label = f'KP {kp_val:.0f}' if kp_val.is_integer() else f'KP {kp_val}'
                        reverse_kp = round((total_length - next_kp_dist) / 1000, 3)
                        attributes = [source_line_name, label, kp_val, reverse_kp, interval_km]
                        if raster_sampler:
                            point_xy = QgsPointXY(x, y)
                            depth_value, res = raster_sampler.sample(point_xy, 1)
                            if res:
                                attributes.append(float(depth_value))
                            else:
                                attributes.append(None)
                                null_depth_count += 1
                        feat.setAttributes(attributes)
                        sink.addFeature(feat, QgsFeatureSink.FastInsert)
                        points_placed += 1

                        next_kp_dist += interval_m
                    
                    cumulative_length += segment_length

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
4.  **Run:** Execute the tool. A new point layer will be created with points at each specified interval. The points will have attributes for KP, reverse KP, the interval distance, and depth (if a raster was provided).
""")

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
