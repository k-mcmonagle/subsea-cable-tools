# kp_range_highlighter_algorithm.py
# -*- coding: utf-8 -*-
"""
KPRangeHighlighterAlgorithm
This tool highlights KP ranges along a path.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingException,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterString,
                       QgsFeature,
                       QgsField,
                       QgsFields)
from .algorithm_base import SubseaCableAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING, PROCESSING_NUMBER_DOUBLE

from ..kp_range_utils import (
    make_distance_area,
    add_distance_mode_parameter,
    read_distance_mode,
)
from ..kp_geo_utils import get_features_skip_invalid, ordered_route_geometry
from .kp_common import START_KP_HELP, add_start_kp_parameter, build_route, read_start_kp, report_kp_basis

class KPRangeHighlighterAlgorithm(SubseaCableAlgorithm):
    """
    This algorithm highlights sections of an RPL line based on user-defined
    Kilometer Points (KPs).
    """

    OUTPUT = 'OUTPUT'
    INPUT = 'INPUT'
    START_KP = 'START_KP'
    END_KP = 'END_KP'
    CUSTOM_LABEL = 'CUSTOM_LABEL'
    # The route's start KP (START_KP is this tool's range start).
    ROUTE_START_KP = 'ROUTE_START_KP'

    def initAlgorithm(self, config=None):
        # Input RPL line layer
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Reference Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        # Start KP
        self.addParameter(
            QgsProcessingParameterNumber(
                self.START_KP,
                self.tr('Range start KP'),
                type=PROCESSING_NUMBER_DOUBLE,
                defaultValue=0.0
            )
        )
        # End KP
        self.addParameter(
            QgsProcessingParameterNumber(
                self.END_KP,
                self.tr('Range end KP'),
                type=PROCESSING_NUMBER_DOUBLE,
                defaultValue=1.0
            )
        )
        # Custom Label
        self.addParameter(
            QgsProcessingParameterString(
                self.CUSTOM_LABEL,
                self.tr('Custom Label'),
                optional=True
            )
        )
        # Output layer
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output layer')
            )
        )

        add_distance_mode_parameter(self)
        add_start_kp_parameter(self, self.ROUTE_START_KP)

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        start_kp = self.parameterAsDouble(parameters, self.START_KP, context)
        end_kp = self.parameterAsDouble(parameters, self.END_KP, context)
        custom_label = self.parameterAsString(parameters, self.CUSTOM_LABEL, context)

        # Only create new fields for the output layer (do not copy source fields)
        fields = QgsFields()
        fields.append(QgsField('start_kp', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('end_kp', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('length_km', FIELD_TYPE_DOUBLE))
        include_custom_label = bool(custom_label and custom_label.strip())
        if include_custom_label:
            fields.append(QgsField('custom_label', FIELD_TYPE_STRING))

        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT,
                                               context, fields, source.wkbType(), source.sourceCrs())

        # Combine all features into a single geometry
        geometries = [f.geometry() for f in get_features_skip_invalid(source)
                      if f.hasGeometry() and not f.geometry().isEmpty()]
        if not geometries:
            return {self.OUTPUT: dest_id}

        # Shared route builder (SeqNo/layer order, no noding).
        combined_geom = ordered_route_geometry(list(get_features_skip_invalid(source)))

        distance_mode = read_distance_mode(self, parameters, context)
        try:
            distance_calculator = make_distance_area(
                source.sourceCrs(), context.transformContext(),
                mode=distance_mode, project=context.project(),
            )
        except ValueError as exc:
            raise QgsProcessingException(str(exc))

        # The slice of the route's KP path between the two KPs (start KP +
        # chainage), clipped to the route.
        route_start_kp = read_start_kp(self, parameters, context, self.ROUTE_START_KP)
        route = build_route([combined_geom], distance_calculator, route_start_kp)
        report_kp_basis(feedback, route, route_start_kp)
        if min(start_kp, end_kp) < route.start_kp_km - 1e-9:
            # As before for a KP below 0: a range before the route start is
            # not highlighted (a range past the end is clipped to it).
            feedback.pushWarning(
                f"KP range {start_kp}-{end_kp} starts before the start of the route "
                f"(KP {route.start_kp_km:.3f}); nothing highlighted.")
            seg_geom = None
        else:
            seg_geom = route.extract_segment(start_kp, end_kp)
        if seg_geom and not seg_geom.isEmpty():
            new_feature = QgsFeature(fields)
            new_feature.setGeometry(seg_geom)
            # Set start_kp, end_kp, length_km, and custom_label if provided
            length_km = end_kp - start_kp
            attrs = [start_kp, end_kp, length_km]
            if include_custom_label:
                attrs.append(custom_label)
            new_feature.setAttributes(attrs)
            sink.addFeature(new_feature, QgsFeatureSink.FastInsert)

        feedback.setProgress(100)

        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
This tool highlights a specific section of a line layer based on start and end Kilometer Points (KPs).

**Instructions:**

1.  **Select Line Layer:** Choose the line layer you want to process from the 'Input RPL Line Layer' dropdown. The tool will automatically handle lines made of multiple segments.
2.  **Enter KP Range:**
    *   **Range start KP:** Type the starting KP for the section you want to highlight.
    *   **Range end KP:** Type the ending KP for the section.
3.  **Add Label (Optional):** You can add a custom text label to the output feature. This is useful for identification.
4.  **Run:** Execute the tool. The output will be a new line layer containing only the highlighted segment.

The section follows the route's KP path: between route vertices along the geodesic (or the straight grid line for \
grid KP), not along the line as drawn in latitude/longitude.
""") + "\n\n" + self.tr(START_KP_HELP)

    def name(self):
        return 'kp_range_highlighter'

    def displayName(self):
        return self.tr('KP Range Highlighter')

    def group(self):
        # Updated to return the desired group name.
        return self.tr('KP Ranges')

    def groupId(self):
        # Updated to return a unique id for the desired group.
        return 'kp_ranges'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return KPRangeHighlighterAlgorithm()
