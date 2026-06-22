# subsea_cable_processing_provider.py
# -*- coding: utf-8 -*-
"""
SubseaCableProcessingProvider
This provider loads processing algorithms for Subsea Cable Tools.
"""

import importlib
import traceback

from qgis.core import QgsProcessingProvider, QgsMessageLog, Qgis
from ..qgis_compat import MESSAGE_INFO, MESSAGE_WARNING


class SubseaCableProcessingProvider(QgsProcessingProvider):

    def __init__(self):
        """Default constructor."""
        super().__init__()

    def unload(self):
        """Unloads the provider (tear-down steps, if any)."""
        pass

    def loadAlgorithms(self):
        def safe_add(module_name: str, class_name: str) -> None:
            try:
                module = importlib.import_module(f'.{module_name}', package=__package__)
                algorithm_class = getattr(module, class_name)
                self.addAlgorithm(algorithm_class())
            except Exception:
                QgsMessageLog.logMessage(
                    f'Failed to register algorithm {class_name} from {module_name}.\n{traceback.format_exc()}',
                    'Subsea Cable Tools',
                    MESSAGE_WARNING,
                )

        QgsMessageLog.logMessage('Loading Subsea Cable Tools algorithms...', 'Subsea Cable Tools', MESSAGE_INFO)

        # Algorithms are listed by toolbox group, then alphabetically by class
        # name within each group. Adding a new algorithm? Slot it into the right
        # group block to keep diffs small.

        # --- KP Ranges ---
        safe_add('kp_range_csv_algorithm', 'KPRangeCSVAlgorithm')
        safe_add('kp_range_depth_slope_summary_algorithm', 'KPRangeDepthSlopeSummaryAlgorithm')
        safe_add('kp_range_extract_rule_based_algorithm', 'ExtractKPRangesRuleBasedAlgorithm')
        safe_add('kp_range_group_adjacent_algorithm', 'KPRangeGroupAdjacentAlgorithm')
        safe_add('kp_range_highlighter_algorithm', 'KPRangeHighlighterAlgorithm')
        safe_add('kp_range_merge_tables_algorithm', 'KPRangeMergeTablesAlgorithm')

        # --- KP Points ---
        safe_add('add_depth_to_point_layer_algorithm', 'AddDepthToPointLayerAlgorithm')
        safe_add('nearest_kp_algorithm', 'NearestKPAlgorithm')
        safe_add('place_kp_points_algorithm', 'PlaceKpPointsAlgorithm')
        safe_add('place_kp_points_from_csv_algorithm', 'PlaceKpPointsFromCsvAlgorithm')
        safe_add('place_single_kp_point_algorithm', 'PlaceSingleKpPointAlgorithm')

        # --- RPL Tools ---
        safe_add('extract_ac_points_algorithm', 'ExtractACPointsAlgorithm')
        safe_add('identify_rpl_area_listing_algorithm', 'IdentifyRPLAreaListingAlgorithm')
        safe_add('identify_rpl_crossing_points_algorithm', 'IdentifyRPLCrossingPointsAlgorithm')
        safe_add('identify_rpl_lay_corridor_proximity_listing_algorithm', 'IdentifyRPLLayCorridorProximityListingAlgorithm')
        safe_add('import_excel_rpl_algorithm', 'ImportExcelRPLAlgorithm')
        safe_add('rpl_route_comparison_algorithm', 'RPLRouteComparisonAlgorithm')
        safe_add('seabed_length_algorithm', 'SeabedLengthAlgorithm')
        safe_add('translate_kp_from_rpl_to_rpl_algorithm', 'TranslateKPFromRPLToRPLAlgorithm')

        # --- Cable Lay Data Import ---
        safe_add('create_cable_lay_geopackage_algorithm', 'CreateCableLayGeoPackageAlgorithm')
        safe_add('import_cable_lay_algorithm', 'ImportCableLayAlgorithm')
        safe_add('import_event_log_algorithm', 'ImportEventLogAlgorithm')
        safe_add('import_slack_log_algorithm', 'ImportSlackLogAlgorithm')
        safe_add('import_body_log_algorithm', 'ImportBodyLogAlgorithm')
        safe_add('import_3d_model_solutions_algorithm', 'Import3DModelSolutionsAlgorithm')
        safe_add('import_as_laid_algorithm', 'ImportAsLaidAlgorithm')
        safe_add('import_plough_data_algorithm', 'ImportPloughDataAlgorithm')

        # --- MDB Tools ---
        safe_add('import_mdb_algorithm', 'ImportMdbAlgorithm')

        # --- MBES Tools ---
        safe_add('create_mbes_raster_from_xyz_algorithm', 'CreateMBESRasterFromXYZAlgorithm')
        safe_add('merge_mbes_rasters_algorithm', 'MergeMBESRastersAlgorithm')

        # --- Other Tools ---
        safe_add('dynamic_buffer_lay_corridor_algorithm', 'DynamicBufferLayCorridorAlgorithm')
        safe_add('export_kp_section_chartlets_algorithm', 'ExportKPSectionChartletsAlgorithm')
        safe_add('extract_lines_intersecting_polygons_algorithm', 'ExtractLinesIntersectingPolygonsAlgorithm')
        safe_add('import_ship_outline_algorithm', 'ImportShipOutlineAlgorithm')
        safe_add('place_ship_outlines_algorithm', 'PlaceShipOutlinesAlgorithm')
        safe_add('plot_line_segments_from_table_algorithm', 'PlotLineSegmentsFromTableAlgorithm')


    def id(self):
        """
        Returns the unique provider id.
        """
        return 'subsea_cable_processing'

    def name(self):
        """
        Returns the provider name.
        """
        return self.tr('Subsea Cable Tools')

    def icon(self):
        """
        Returns a QIcon for the provider.
        """
        return QgsProcessingProvider.icon(self)

    def longName(self):
        """
        Returns a longer version of the provider name.
        """
        return self.name()

    def tr(self, string):
        from qgis.PyQt.QtCore import QCoreApplication
        return QCoreApplication.translate('SubseaCableProcessingProvider', string)
