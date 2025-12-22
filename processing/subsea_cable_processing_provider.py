# subsea_cable_processing_provider.py
# -*- coding: utf-8 -*-
"""
SubseaCableProcessingProvider
This provider loads processing algorithms for Subsea Cable Tools.
"""

from qgis.core import QgsProcessingProvider
from .kp_range_highlighter_algorithm import KPRangeHighlighterAlgorithm
from .kp_range_csv_algorithm import KPRangeCSVAlgorithm
from .kp_range_merge_tables_algorithm import KPRangeMergeTablesAlgorithm
from .import_excel_rpl_algorithm import ImportExcelRPLAlgorithm
from .import_cable_lay_algorithm import ImportCableLayAlgorithm

from .nearest_kp_algorithm import NearestKPAlgorithm
from .import_bathy_mdb_algorithm import ImportBathyMdbAlgorithm
from .place_kp_points_algorithm import PlaceKpPointsAlgorithm
from .place_kp_points_from_csv_algorithm import PlaceKpPointsFromCsvAlgorithm
from .place_single_kp_point_algorithm import PlaceSingleKpPointAlgorithm
from .create_mbes_raster_from_xyz_algorithm import CreateMBESRasterFromXYZAlgorithm
from .merge_mbes_rasters_algorithm import MergeMBESRastersAlgorithm

from .import_ship_outline_algorithm import ImportShipOutlineAlgorithm
from .place_ship_outlines_algorithm import PlaceShipOutlinesAlgorithm
from .plot_line_segments_from_table_algorithm import PlotLineSegmentsFromTableAlgorithm
from .translate_kp_from_rpl_to_rpl_algorithm import TranslateKPFromRPLToRPLAlgorithm
from .rpl_route_comparison_algorithm import RPLRouteComparisonAlgorithm
from .seabed_length_algorithm import SeabedLengthAlgorithm
from .extract_ac_points_algorithm import ExtractACPointsAlgorithm
from .identify_rpl_crossing_points_algorithm import IdentifyRPLCrossingPointsAlgorithm
from .dynamic_buffer_lay_corridor_algorithm import DynamicBufferLayCorridorAlgorithm
from .identify_rpl_area_listing_algorithm import IdentifyRPLAreaListingAlgorithm
from .identify_rpl_lay_corridor_proximity_listing_algorithm import IdentifyRPLLayCorridorProximityListingAlgorithm


class SubseaCableProcessingProvider(QgsProcessingProvider):

    def __init__(self):
        """Default constructor."""
        super().__init__()

    def unload(self):
        """Unloads the provider (tear-down steps, if any)."""
        pass

    def loadAlgorithms(self):
        print('Loading Subsea Cable Tools algorithms...')
        self.addAlgorithm(KPRangeHighlighterAlgorithm())
        self.addAlgorithm(KPRangeCSVAlgorithm())
        self.addAlgorithm(KPRangeMergeTablesAlgorithm())
        self.addAlgorithm(ImportExcelRPLAlgorithm())
        self.addAlgorithm(NearestKPAlgorithm())
        self.addAlgorithm(ImportBathyMdbAlgorithm())
        self.addAlgorithm(PlaceKpPointsAlgorithm())
        self.addAlgorithm(PlaceKpPointsFromCsvAlgorithm())
        self.addAlgorithm(PlaceSingleKpPointAlgorithm())
        print('Registering CreateMBESRasterFromXYZAlgorithm...')
        self.addAlgorithm(CreateMBESRasterFromXYZAlgorithm())
        print('Registering MergeMBESRastersAlgorithm...')
        self.addAlgorithm(MergeMBESRastersAlgorithm())
        print('Registering ImportCableLayAlgorithm...')
        self.addAlgorithm(ImportCableLayAlgorithm())
        print('Registering ImportShipOutlineAlgorithm...')
        self.addAlgorithm(ImportShipOutlineAlgorithm())
        print('Registering PlaceShipOutlinesAlgorithm...')
        self.addAlgorithm(PlaceShipOutlinesAlgorithm())
        print('Registering PlotLineSegmentsFromTableAlgorithm...')
        self.addAlgorithm(PlotLineSegmentsFromTableAlgorithm())
        print('Registering TranslateKPFromRPLToRPLAlgorithm...')
        self.addAlgorithm(TranslateKPFromRPLToRPLAlgorithm())
        print('Registering RPLRouteComparisonAlgorithm...')
        self.addAlgorithm(RPLRouteComparisonAlgorithm())
        print('Registering SeabedLengthAlgorithm...')
        self.addAlgorithm(SeabedLengthAlgorithm())
        print('Registering DynamicBufferLayCorridorAlgorithm...')
        self.addAlgorithm(DynamicBufferLayCorridorAlgorithm())
        print('Registering ExtractACPointsAlgorithm...')
        self.addAlgorithm(ExtractACPointsAlgorithm())
        print('Registering IdentifyRPLCrossingPointsAlgorithm...')
        self.addAlgorithm(IdentifyRPLCrossingPointsAlgorithm())
        print('Registering IdentifyRPLAreaListingAlgorithm...')
        self.addAlgorithm(IdentifyRPLAreaListingAlgorithm())
        print('Registering IdentifyRPLLayCorridorProximityListingAlgorithm...')
        self.addAlgorithm(IdentifyRPLLayCorridorProximityListingAlgorithm())


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
