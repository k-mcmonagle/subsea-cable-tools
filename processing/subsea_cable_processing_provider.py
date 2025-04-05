# subsea_cable_processing_provider.py
# -*- coding: utf-8 -*-
"""
SubseaCableProcessingProvider
This provider loads processing algorithms for Subsea Cable Tools.
"""

from qgis.core import QgsProcessingProvider
from .kp_range_highlighter_algorithm import KPRangeHighlighterAlgorithm
from .kp_range_csv_algorithm import KPRangeCSVAlgorithm
from .import_excel_rpl_algorithm import ImportExcelRPLAlgorithm
from .nearest_kp_algorithm import NearestKPAlgorithm
from .import_bathy_mdb_algorithm import ImportBathyMdbAlgorithm


class SubseaCableProcessingProvider(QgsProcessingProvider):

    def __init__(self):
        """Default constructor."""
        super().__init__()

    def unload(self):
        """Unloads the provider (tear-down steps, if any)."""
        pass

    def loadAlgorithms(self):
        self.addAlgorithm(KPRangeHighlighterAlgorithm())
        self.addAlgorithm(KPRangeCSVAlgorithm())
        self.addAlgorithm(ImportExcelRPLAlgorithm())
        self.addAlgorithm(NearestKPAlgorithm())
        self.addAlgorithm(ImportBathyMdbAlgorithm())


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
